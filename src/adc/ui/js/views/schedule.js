/*
 * views/schedule.js -- ADC.views.schedule. Automated maintenance scheduling (SPEC 6.5).
 *
 * Configures and controls automated unattended disk cleaning via the Windows Task
 * Scheduler COM interface (Schedule.Service).
 *
 * Invariants:
 *   - Only SAFE-tier targets are allowed in unattended runs. The engine raises
 *     NonSafeTargetError if any caution or dangerous target is submitted, and the UI
 *     only ever offers targets from catalogue.schedulable.
 *   - The task runs headless (`--headless --preset=safe`), evaluates free disk space
 *     threshold if requested, writes a JSON audit report, and alerts via Windows toast.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;

  var DOW_KEYS = [
    { day: 1, i18n: 'schedule.dow.mon' },
    { day: 2, i18n: 'schedule.dow.tue' },
    { day: 3, i18n: 'schedule.dow.wed' },
    { day: 4, i18n: 'schedule.dow.thu' },
    { day: 5, i18n: 'schedule.dow.fri' },
    { day: 6, i18n: 'schedule.dow.sat' },
    { day: 0, i18n: 'schedule.dow.sun' }
  ];

  var host = null;
  var body = null;
  var statusLine = null;
  var active = false;

  var state = {
    loading: false,
    saving: false,
    status: null,
    catalog: null,
    form: {
      enabled: false,
      frequency: 'daily',
      time_of_day: '09:00',
      days_of_week: [1],
      threshold_pct: 15,
      preset: 'safe',
      targets: [],
      notify: true
    },
    flash: null,
    flashTimer: null
  };

  /* --- helpers ------------------------------------------------------------------- */

  function setFlash(key, tone) {
    if (state.flashTimer) {
      window.clearTimeout(state.flashTimer);
      state.flashTimer = null;
    }
    state.flash = { key: key, tone: tone || 'success' };
    updateStatusLine();
    state.flashTimer = window.setTimeout(function () {
      state.flash = null;
      state.flashTimer = null;
      updateStatusLine();
    }, 4000);
  }

  function updateStatusLine() {
    if (!statusLine) { return; }
    ui.clear(statusLine);
    if (state.flash) {
      statusLine.className = state.flash.tone === 'warn'
        ? 'sched__status-msg sched__status-warn'
        : 'sched__status-msg';
      statusLine.appendChild(ui.el('span', { i18n: state.flash.key }));
      i18n.apply(statusLine);
    } else {
      statusLine.className = 'sched__status-msg';
    }
  }

  function syncFormFromStatus(st) {
    if (!st) { return; }
    state.form.enabled = st.enabled === true;
    state.form.frequency = st.frequency || 'daily';
    state.form.time_of_day = st.time_of_day || '09:00';
    state.form.days_of_week = (st.days_of_week && st.days_of_week.length) ? st.days_of_week.slice() : [1];
    state.form.threshold_pct = typeof st.threshold_pct === 'number' ? st.threshold_pct : 15;
    state.form.preset = 'safe';
    state.form.targets = (st.targets && st.targets.length) ? st.targets.slice() : [];
    state.form.notify = st.notify !== false;
  }

  /* --- API operations ------------------------------------------------------------ */

  function loadStatus() {
    state.loading = true;
    api.scheduleGet().then(function (res) {
      state.status = res && res.status ? res.status : null;
      syncFormFromStatus(state.status);
      state.loading = false;
      if (active) { render(); }
    }).catch(function (err) {
      state.loading = false;
      ui.showError(err);
      if (active) { render(); }
    });
  }

  function onSave() {
    if (state.saving) { return; }
    state.saving = true;
    render();

    var payload = {
      enabled: state.form.enabled,
      frequency: state.form.frequency,
      time_of_day: state.form.time_of_day,
      days_of_week: state.form.days_of_week,
      threshold_pct: state.form.threshold_pct,
      preset: 'safe',
      targets: state.form.targets,
      notify: state.form.notify
    };

    api.scheduleSet(payload).then(function (res) {
      state.saving = false;
      state.status = res && res.status ? res.status : null;
      syncFormFromStatus(state.status);
      setFlash(state.form.enabled ? 'schedule.saved' : 'schedule.disabled', 'success');
      if (active) { render(); }
    }).catch(function (err) {
      state.saving = false;
      setFlash('schedule.error', 'warn');
      ui.showError(err);
      if (active) { render(); }
    });
  }

  function onRunNow() {
    api.scheduleRunNow().then(function () {
      setFlash('schedule.triggered', 'info');
      loadStatus();
    }).catch(function (err) {
      ui.showError(err);
    });
  }

  function onDisable() {
    api.scheduleDelete().then(function (res) {
      state.status = res && res.status ? res.status : null;
      syncFormFromStatus(state.status);
      setFlash('schedule.disabled', 'info');
      if (active) { render(); }
    }).catch(function (err) {
      ui.showError(err);
    });
  }

  /* --- card builders ------------------------------------------------------------- */

  function buildStatusCard() {
    var st = state.status || {};
    var isEnabled = st.enabled === true;
    var stateKey = isEnabled ? 'schedule.status.active' : 'schedule.status.disabled';
    var stateTone = isEnabled ? 'success' : 'info';
    if (st.state === 'running') {
      stateKey = 'schedule.status.running';
      stateTone = 'warn';
    } else if (!st.installed && isEnabled) {
      stateKey = 'schedule.status.not_configured';
      stateTone = 'warn';
    }

    var badge = ui.el('span', { class: 'tag tag--' + stateTone, i18n: stateKey });

    var nextRunVal = st.next_run_time || null;
    var lastRunVal = st.last_run_time || null;
    var lastResVal = null;
    if (st.last_result === 0) {
      lastResVal = 'schedule.success';
    } else if (typeof st.last_result === 'number') {
      lastResVal = String(st.last_result);
    }

    var grid = ui.el('div', { class: 'sched__info-grid' }, [
      ui.el('div', { class: 'sched__info-item' }, [
        ui.el('span', { class: 'sched__info-label', i18n: 'schedule.next_run' }),
        nextRunVal ? ui.el('span', { class: 'sched__info-val mono', text: nextRunVal })
                   : ui.el('span', { class: 'sched__info-val', i18n: 'schedule.never' })
      ]),
      ui.el('div', { class: 'sched__info-item' }, [
        ui.el('span', { class: 'sched__info-label', i18n: 'schedule.last_run' }),
        lastRunVal ? ui.el('span', { class: 'sched__info-val mono', text: lastRunVal })
                   : ui.el('span', { class: 'sched__info-val', i18n: 'schedule.never' })
      ]),
      ui.el('div', { class: 'sched__info-item' }, [
        ui.el('span', { class: 'sched__info-label', i18n: 'schedule.last_result' }),
        lastResVal ? (lastResVal === 'schedule.success' ? ui.el('span', { class: 'sched__info-val', i18n: lastResVal })
                                                       : ui.el('span', { class: 'sched__info-val mono', text: lastResVal }))
                   : ui.el('span', { class: 'sched__info-val', i18n: 'schedule.never' })
      ])
    ]);

    var actions = [
      ui.btn({
        i18n: 'schedule.btn.run_now',
        icon: 'icon-play',
        variant: 'ghost',
        on: { click: onRunNow }
      }),
      ui.btn({
        i18n: 'schedule.btn.refresh',
        icon: 'icon-refresh',
        variant: 'ghost',
        on: { click: loadStatus }
      })
    ];
    if (isEnabled) {
      actions.push(ui.btn({
        i18n: 'schedule.btn.disable',
        icon: 'icon-stop',
        variant: 'danger',
        on: { click: onDisable }
      }));
    }

    var card = ui.card({
      i18n: 'schedule.card.status',
      sub: 'schedule.card.status.sub',
      icon: 'icon-schedule',
      class: 'sched__card'
    });

    var row = ui.el('div', { class: 'sched__status-row' }, [badge, ui.el('div', { class: 'sched__actions' }, actions)]);
    card.body.appendChild(row);
    card.body.appendChild(grid);
    return card;
  }

  function buildConfigCard() {
    var card = ui.card({
      i18n: 'schedule.card.config',
      sub: 'schedule.card.config.sub',
      icon: 'icon-settings',
      class: 'sched__card'
    });

    var fields = ui.el('div', { class: 'sched__fields' });

    /* Enable checkbox */
    var enableBox = ui.el('input', {
      type: 'checkbox',
      class: 'sched__check',
      attrs: { checked: state.form.enabled ? 'checked' : null },
      on: {
        change: function (evt) {
          state.form.enabled = evt.target.checked;
        }
      }
    });
    fields.appendChild(ui.field({
      i18n: 'schedule.field.enabled',
      help: 'schedule.field.enabled.help',
      control: enableBox
    }));

    /* Frequency select */
    var freqSelect = ui.el('select', {
      class: 'sched__select',
      on: {
        change: function (evt) {
          state.form.frequency = evt.target.value;
          render();
        }
      }
    }, [
      ui.el('option', { value: 'daily', i18n: 'schedule.freq.daily', attrs: state.form.frequency === 'daily' ? { selected: 'selected' } : null }),
      ui.el('option', { value: 'weekly', i18n: 'schedule.freq.weekly', attrs: state.form.frequency === 'weekly' ? { selected: 'selected' } : null }),
      ui.el('option', { value: 'threshold', i18n: 'schedule.freq.threshold', attrs: state.form.frequency === 'threshold' ? { selected: 'selected' } : null })
    ]);
    fields.appendChild(ui.field({
      i18n: 'schedule.field.frequency',
      help: 'schedule.field.frequency.help',
      control: freqSelect
    }));

    /* Scheduled time */
    var timeInput = ui.el('input', {
      type: 'text',
      class: 'sched__time mono',
      attrs: { value: state.form.time_of_day, maxlength: '5', placeholder: '09:00' },
      on: {
        input: function (evt) {
          state.form.time_of_day = evt.target.value;
        }
      }
    });
    fields.appendChild(ui.field({
      i18n: 'schedule.field.time',
      help: 'schedule.field.time.help',
      control: timeInput
    }));

    /* Day of week buttons (for weekly) */
    if (state.form.frequency === 'weekly') {
      var daysWrap = ui.el('div', { class: 'sched__days' });
      for (var i = 0; i < DOW_KEYS.length; i += 1) {
        (function (item) {
          var isSel = state.form.days_of_week.indexOf(item.day) !== -1;
          var btn = ui.el('button', {
            type: 'button',
            class: isSel ? 'sched__day-btn is-active' : 'sched__day-btn',
            i18n: item.i18n,
            on: {
              click: function () {
                var idx = state.form.days_of_week.indexOf(item.day);
                if (idx !== -1) {
                  if (state.form.days_of_week.length > 1) {
                    state.form.days_of_week.splice(idx, 1);
                  }
                } else {
                  state.form.days_of_week.push(item.day);
                }
                render();
              }
            }
          });
          daysWrap.appendChild(btn);
        })(DOW_KEYS[i]);
      }
      fields.appendChild(ui.field({
        i18n: 'schedule.field.days',
        help: 'schedule.field.days.help',
        control: daysWrap
      }));
    }

    /* Threshold select */
    var threshSelect = ui.el('select', {
      class: 'sched__select',
      on: {
        change: function (evt) {
          state.form.threshold_pct = parseInt(evt.target.value, 10) || 15;
        }
      }
    }, [
      ui.el('option', { value: '10', i18n: 'schedule.threshold.10', attrs: state.form.threshold_pct === 10 ? { selected: 'selected' } : null }),
      ui.el('option', { value: '15', i18n: 'schedule.threshold.15', attrs: state.form.threshold_pct === 15 ? { selected: 'selected' } : null }),
      ui.el('option', { value: '20', i18n: 'schedule.threshold.20', attrs: state.form.threshold_pct === 20 ? { selected: 'selected' } : null })
    ]);
    fields.appendChild(ui.field({
      i18n: 'schedule.field.threshold',
      help: 'schedule.field.threshold.help',
      control: threshSelect
    }));

    /* Notification checkbox */
    var notifyBox = ui.el('input', {
      type: 'checkbox',
      class: 'sched__check',
      attrs: { checked: state.form.notify ? 'checked' : null },
      on: {
        change: function (evt) {
          state.form.notify = evt.target.checked;
        }
      }
    });
    fields.appendChild(ui.field({
      i18n: 'schedule.field.notify',
      help: 'schedule.field.notify.help',
      control: notifyBox
    }));

    card.body.appendChild(fields);
    return card;
  }

  function buildTargetsCard() {
    var card = ui.card({
      i18n: 'schedule.card.targets',
      sub: 'schedule.card.targets.sub',
      icon: 'icon-safe',
      class: 'sched__card'
    });

    /* Safety notice */
    var notice = ui.el('div', { class: 'sched__notice' }, [
      ui.icon('icon-info', 'sched__notice-ico'),
      ui.el('p', { class: 'sched__notice-text', i18n: 'schedule.notice.safe_only' })
    ]);
    card.body.appendChild(notice);

    /* Schedulable items summary */
    var cat = ADC.app.catalog();
    var schedIds = (cat && cat.schedulable) ? cat.schedulable : [];
    var targetList = ui.el('div', { class: 'sched__targets' });

    /* Preset summary tile */
    var presetItem = ui.el('div', { class: 'sched__target-item' }, [
      ui.icon('icon-safe', 'risk__ico'),
      ui.el('span', { class: 'sched__target-name', i18n: 'schedule.targets.all_safe' }),
      ui.el('span', { class: 'tag tag--success', text: String(schedIds.length) })
    ]);
    targetList.appendChild(presetItem);

    card.body.appendChild(targetList);

    /* Save button */
    var saveBtn = ui.btn({
      i18n: 'schedule.btn.save',
      variant: 'primary',
      disabled: state.saving,
      on: { click: onSave }
    });
    var actionsWrap = ui.el('div', { class: 'sched__actions' }, [saveBtn]);
    card.body.appendChild(actionsWrap);

    return card;
  }

  /* --- main render --------------------------------------------------------------- */

  function render() {
    if (!body) { return; }
    ui.clear(body);

    body.appendChild(buildStatusCard());
    body.appendChild(buildConfigCard());
    body.appendChild(buildTargetsCard());

    i18n.apply(body);
    updateStatusLine();
  }

  /* --- lifecycle ----------------------------------------------------------------- */

  function mount(hostEl) {
    host = hostEl;
    statusLine = ui.el('div', { class: 'sched__status-msg' });
    body = ui.el('div', { class: 'sched__body' });
    host.appendChild(ui.el('div', { class: 'sched' }, [statusLine, body]));
  }

  function enter(hostEl, actions) {
    active = true;
    if (hostEl) { host = hostEl; }
    loadStatus();
    render();
    return actions;
  }

  function leave() {
    active = false;
    if (state.flashTimer) {
      window.clearTimeout(state.flashTimer);
      state.flashTimer = null;
    }
  }

  function relang() {
    render();
  }

  views.schedule = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
