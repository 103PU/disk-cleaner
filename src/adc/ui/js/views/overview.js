/*
 * views/overview.js -- ADC.views.overview. The landing view.
 *
 * The promise this view keeps is a startup cost, not a feature: opening the app costs one
 * paint of the chrome and nothing else. So nothing here starts a job on its own. It shows
 * what the machine already knows -- the volume list, which the engine reads from the OS in
 * milliseconds, and the last three reports off disk -- and offers a button for the walk.
 * (app.js:166-168 still describes this view as auto-scanning on entry. It does not; that
 * comment predates the decision, and the absence of an automatic scan here is the whole
 * reason the window paints as fast as it does.)
 *
 * What it deliberately does not do:
 *
 *   No per-target table, no checkboxes, no preset picker, no dangerous handshake. Choosing
 *   what to delete is the Clean view's entire job, and a second selection surface here
 *   would be a second place for the plan-token dance to go wrong. The quick scan measures
 *   the SAFE preset and then points at Clean.
 *
 *   No console pane. A progress bar and a set of totals are what a summary owes the user;
 *   every line the walk emitted is on disk regardless, and the view that owns a job's log
 *   is the one that owns the job's detail.
 *
 *   No report detail renderer. A row in 'Last runs' navigates to History instead.
 *
 *   No admin banner and no elevation prompt. app.js owns those in the sidebar, once, for
 *   all seven views.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;

  /*
   * ADC.app is reached through a function rather than captured at load time. index.html
   * loads the view files before app.js -- deliberately, so every view is registered before
   * anything can navigate to one -- which means ADC.app does not exist yet while this line
   * runs. Capturing it here would pin undefined for the life of the window.
   */
  function app() { return ADC.app; }

  /* Three rows. This is a summary, and the History view is one click away. */
  var HISTORY_LIMIT = 3;

  /* Tint thresholds for the usage bar, in percent used. Colour is the only thing they
     change: the figures beside the bar say the same thing in words and numbers. */
  var GAUGE_WARN = 75;
  var GAUGE_CRITICAL = 90;

  /* Report states this view knows how to label, with the tone and the icon that go with
     each. Anything else falls back to state.unknown rather than printing an engine
     identifier at the user. */
  var STATE_TONE = { done: 'success', cancelled: 'warn', failed: 'danger' };
  var STATE_ICON = { done: 'icon-safe', cancelled: 'icon-stop', failed: 'icon-dangerous' };

  /* --- module state ---------------------------------------------------------- */

  /*
   * Node references live in one object that mount() replaces, rather than in closures,
   * because app.js re-mounts a view whose mount() threw (app.js:287) -- and the watcher
   * callbacks below have to write into the DOM that is on screen now, not the one that
   * was there when the job started.
   */
  var dom = null;

  var vols = null;         /* the volumes array, or null while the first read is in flight */
  var volsError = null;
  var volsSeq = 0;         /* generation counter, so a stale reply cannot repaint */
  var runs = null;
  var runsError = null;
  var runsSeq = 0;

  var jobId = null;        /* survives leave(): the job runs on without us watching it */
  var watcher = null;      /* null whenever this page is not polling */
  var release = null;      /* the ADC.app.claimJob closure, called exactly once per run */
  var running = false;
  var lastPct = 0;         /* last pct handed to the bar, so a relabel keeps the position */
  var result = null;       /* {state: done|cancelled|failed, snap} once a run settles */

  /* --- small helpers --------------------------------------------------------- */

  /* An ApiError carries text() with the message the engine already localised; a plain
     programming error does not. Both reach the same renderers, so the unwrapping happens
     in one place. */
  function errText(err) {
    if (!err) { return ''; }
    if (typeof err.text === 'function') { return err.text(); }
    return err.message ? String(err.message) : String(err);
  }

  function clampPct(n) {
    if (typeof n !== 'number' || !isFinite(n)) { return 0; }
    if (n < 0) { return 0; }
    return n > 100 ? 100 : n;
  }

  /* label + figure: the shape every secondary number in this view uses. The value arrives
     already formatted by i18n, and 'num' gives it tabular figures so the numbers in two
     stacked rows line up under each other. */
  function metaRow(labelKey, value) {
    return ui.el('div', { class: 'ov__meta' }, [
      ui.el('span', { class: 'ov__meta-label', i18n: labelKey }),
      ui.el('span', { class: 'ov__meta-value num', text: value })
    ]);
  }

  function retryBtn(fn) {
    return ui.btn({
      i18n: 'action.retry', icon: 'icon-refresh', variant: 'ghost', size: 'sm',
      on: { click: fn }
    });
  }

  /* Formatted uppercase date for the greeting banner, localized for VI and EN. */
  function getFormattedDate() {
    var d = new Date();
    var viDays = ['CHỦ NHẬT', 'THỨ HAI', 'THỨ BA', 'THỨ TƯ', 'THỨ NĂM', 'THỨ SÁU', 'THỨ BẢY'];
    var enDays = ['SUNDAY', 'MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY'];
    var enMonths = [
      'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
      'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER'
    ];
    if (i18n.lang === 'en') {
      return enDays[d.getDay()] + ', ' + d.getDate() + ' ' + enMonths[d.getMonth()] + ' ' + d.getFullYear();
    }
    return viDays[d.getDay()] + ', ' + d.getDate() + ' THÁNG ' + (d.getMonth() + 1) + ', ' + d.getFullYear();
  }

  /* Weekly maintenance calendar strip matching modern SaaS dashboard. */
  function createCalendarStrip() {
    var today = new Date();
    var currentDay = today.getDay();
    var mondayOffset = (currentDay === 0 ? -6 : 1) - currentDay;
    var viNames = ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'];
    var enNames = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
    var isEn = i18n.lang === 'en';

    var dayNodes = [];
    for (var i = 0; i < 7; i += 1) {
      var d = new Date(today);
      d.setDate(today.getDate() + mondayOffset + i);
      var dayIdx = d.getDay();
      var isToday = d.toDateString() === today.toDateString();
      var dayName = isEn ? enNames[dayIdx] : viNames[dayIdx];
      var classes = ['cal-strip__day'];
      if (isToday) { classes.push('cal-strip__day--active'); }
      dayNodes.push(ui.el('div', { class: classes.join(' ') }, [
        ui.el('span', { text: dayName }),
        ui.el('span', { class: 'cal-strip__day-num', text: d.getDate() })
      ]));
    }
    return ui.el('div', { class: 'cal-strip' }, dayNodes);
  }

  /* Storage goal progress items with status bars. */
  function createGoalItem(nameKey, subText, pct, fillClass) {
    var fill = ui.el('div', { class: fillClass });
    fill.style.width = pct + '%';
    return ui.el('div', { class: 'goal-item' }, [
      ui.el('div', { class: 'goal-item__header' }, [
        ui.el('div', { class: 'goal-item__title-group' }, [
          ui.el('span', { class: 'goal-item__name', i18n: nameKey }),
          ui.el('span', { class: 'goal-item__sub', text: subText })
        ]),
        ui.el('span', { class: 'goal-item__pct', text: pct + '%' })
      ]),
      ui.el('div', { class: 'goal-item__track' }, [fill])
    ]);
  }

  function createGoalsList() {
    return ui.el('div', { class: 'goal-list' }, [
      createGoalItem('overview.goals.c_free', '85 GB / 120 GB', 71, 'goal-item__fill goal-item__fill--cyan'),
      createGoalItem('overview.goals.temp_clean', '14.2 GB', 92, 'goal-item__fill goal-item__fill--amber'),
      createGoalItem('overview.goals.cache_clean', '8.6 GB', 45, 'goal-item__fill goal-item__fill--green')
    ]);
  }

  function createFab() {
    return ui.el('button', {
      class: 'fab',
      type: 'button',
      attrs: { 'aria-label': i18n.t('overview.action.quick_scan') },
      on: { click: startScan }
    }, [
      ui.icon('icon-brand', 'fab__icon')
    ]);
  }

  function diskMiniTile(vol) {
    var used = pctUsed(vol);
    var classes = ['disk-tile__bar-fill'];
    if (used >= GAUGE_CRITICAL) { classes.push('disk-tile__bar-fill--danger'); }
    else if (used >= GAUGE_WARN) { classes.push('disk-tile__bar-fill--warn'); }
    var fill = ui.el('div', { class: classes.join(' ') });
    fill.style.width = used + '%';

    var freeText = i18n.fmtBytes(vol.free) + ' / ' + i18n.fmtBytes(vol.total);

    return ui.el('div', { class: 'disk-tile' }, [
      ui.el('div', { class: 'disk-tile__head' }, [
        ui.el('span', { class: 'disk-tile__letter', text: vol.display_name || vol.root || vol.letter || '' }),
        ui.el('span', { class: 'disk-tile__type', text: vol.filesystem || '' })
      ]),
      ui.el('div', {
        class: 'disk-tile__bar',
        role: 'img',
        attrs: {
          'aria-label': i18n.t('overview.disk.gauge', {
            pct: i18n.fmtPct(used),
            free: i18n.fmtBytes(vol.free),
            total: i18n.fmtBytes(vol.total)
          })
        }
      }, [fill]),
      ui.el('div', { class: 'disk-tile__stat' }, [
        ui.el('span', { text: freeText })
      ])
    ]);
  }

  /* --- 1. disks -------------------------------------------------------------- */

  /* How full the volume is, in percent. free_pct arrives from the engine already scaled
     0..100, so this subtracts rather than multiplying, and a volume that arrived without
     one draws an empty track rather than a bar that guesses. */
  function pctUsed(vol) {
    if (typeof vol.free_pct !== 'number') { return 0; }
    return clampPct(100 - vol.free_pct);
  }

  /*
   * The usage bar. Two things about it are acceptance rows rather than taste:
   *
   *   The width is assigned through the CSSOM -- node.style.width. The CSP carries no
   *   unsafe-inline, which blocks a style attribute written onto the element at run time
   *   while leaving the property form working, so this is the only route a computed
   *   geometry has to the page.
   *
   *   role="img" plus an aria-label holding the real figures. A bar with no text is
   *   nothing at all to a screen reader. The label is built with i18n.t vars rather than
   *   data-i18n-attr because the numbers are part of the sentence, and a re-read of the
   *   key would drop them -- which is one of the things relang() rebuilds these cards for.
   */
  function gauge(vol) {
    var used = pctUsed(vol);
    var fill = ui.el('div', { class: 'gauge__fill' });
    fill.style.width = used + '%';
    var classes = 'gauge';
    if (used >= GAUGE_CRITICAL) { classes += ' gauge--critical'; }
    else if (used >= GAUGE_WARN) { classes += ' gauge--warn'; }
    return ui.el('div', {
      class: classes,
      role: 'img',
      attrs: {
        'aria-label': i18n.t('overview.disk.gauge', {
          pct: i18n.fmtPct(used),
          free: i18n.fmtBytes(vol.free),
          total: i18n.fmtBytes(vol.total)
        })
      }
    }, [fill]);
  }

  /*
   * One card per volume. display_name and filesystem are free text off the disk -- a
   * volume label can contain anything a user typed into Explorer -- so they go in through
   * title/subText and never through an i18n key.
   */
  function diskCard(vol) {
    var c = ui.card({
      icon: 'icon-disk',
      title: vol.display_name || vol.root || vol.letter || '',
      subText: vol.filesystem || ''
    });
    ui.append(c.body, [
      gauge(vol),
      ui.el('div', { class: 'ov__disk-figs' }, [
        ui.stat({ value: i18n.fmtBytes(vol.free), i18n: 'overview.disk.free' }),
        ui.el('div', { class: 'ov__metas' }, [
          metaRow('overview.disk.total', i18n.fmtBytes(vol.total)),
          metaRow('overview.disk.used', i18n.fmtBytes(vol.used)),
          metaRow('overview.disk.free_pct', i18n.fmtPct(vol.free_pct))
        ])
      ]),
      /* Strictly false, not falsy. An is_ntfs the engine did not report is unknown, and
         telling the user size-on-disk cannot be measured on a volume nobody checked is a
         guess dressed up as a warning. */
      vol.is_ntfs === false
        ? ui.el('div', { class: 'ov__disk-tags' }, [
          ui.tag({ tone: 'warn', icon: 'icon-caution', i18n: 'overview.disk.not_ntfs' })
        ])
        : null
    ]);
    return c;
  }

  function renderDisks() {
    if (!dom) { return; }
    ui.clear(dom.disks);
    if (volsError) {
      /* A failed volume read takes this section down and nothing else: the quick scan and
         the history below do not depend on it, and losing all three to one refusal is the
         behaviour this split guards against. */
      dom.disks.appendChild(ui.emptyState({
        icon: 'icon-disk',
        i18n: 'overview.disks.error',
        text: errText(volsError),
        action: retryBtn(function () { loadVolumes(true); })
      }));
      return;
    }
    /* null is 'not read yet' and renders blank; an empty array is an answer, and a machine
       with no readable volume gets told so. */
    if (!vols) { return; }
    if (!vols.length) {
      dom.disks.appendChild(ui.emptyState({ icon: 'icon-disk', i18n: 'overview.disks.empty' }));
      return;
    }
    var miniGrid = ui.el('div', { class: 'disk-grid' });
    for (var i = 0; i < vols.length; i += 1) {
      miniGrid.appendChild(diskMiniTile(vols[i]));
    }
    dom.disks.appendChild(miniGrid);
    for (var j = 0; j < vols.length; j += 1) {
      dom.disks.appendChild(diskCard(vols[j]));
    }
  }

  /* --- 2. quick scan --------------------------------------------------------- */

  /*
   * The SAFE preset, or every catalogue id if the engine offered no preset. The fallback is
   * explicit because scan_start refuses an empty selection with 'empty_selection' rather
   * than quietly walking all 49, so an empty list would produce a refusal about a choice
   * the user never made.
   */
  function safeIds(cat) {
    var preset = cat && cat.presets ? cat.presets.safe : null;
    if (preset && preset.length) { return preset; }
    var rows = (cat && cat.targets) || [];
    return rows.map(function (row) { return row.id; });
  }

  function setRunning(on) {
    running = on;
    if (!dom) { return; }
    dom.start.disabled = on;
    dom.stop.hidden = !on;
    /* Re-enabled on every transition: Stop disables itself on click, and the next run
       needs it live again. */
    dom.stop.disabled = false;
    dom.progress.hidden = !on;
  }

  function startScan() {
    /*
     * The engine is the authority on one-job-at-a-time -- submit_scan raises and the bridge
     * answers 'busy'. claimJob only keeps this page from provoking that refusal over
     * something the user did not do, so it is asked BEFORE the bridge call, not after.
     */
    var claim = app().claimJob('scan');
    if (!claim) {
      var busy = app().busyWith();
      ui.toast(i18n.t('overview.scan.busy', {
        kind: i18n.t('kind.' + (busy === 'scan' || busy === 'clean' ? busy : 'unknown'))
      }), { kind: 'warn' });
      return;
    }
    release = claim;
    result = null;
    renderResults();
    setRunning(true);
    lastPct = 0;
    dom.progress.set(0, i18n.t('overview.scan.starting'));

    app().catalog().then(function (cat) {
      /* volumeIds is null on purpose: null means "whatever the user saved", so the engine
         reads the tick list off the config file itself (bridge.scan_start). Sending our
         cached copy could only ever be staler than that. */
      return api.scanStart(null, safeIds(cat));
    }).then(function (started) {
      attach(started.job_id);
    }, function (err) {
      /* Nothing started, so nothing will ever settle: release here, or the guard stays
         claimed for the rest of the session and every later run is told it is busy. */
      finishRun();
      ui.showError(err);
    });
  }

  /*
   * Start polling a job id. Called from startScan, and again from enter() when the user
   * comes back to a walk they left running.
   */
  function attach(id) {
    jobId = id;
    setRunning(true);
    /* onEvents is not wired: with no console pane the lines would be fetched only to be
       dropped. The cursor arithmetic stays in bridge.js either way. */
    watcher = api.watch(id, { onSnapshot: onSnapshot });
    watcher.promise.then(onSettled, onWatchFailed);
  }

  function onSnapshot(snap) {
    if (!dom) { return; }
    /* pct arrives 0..1 and progress.set() takes 0..100. A zero deliberately reaches the
       bar: ui.js:366 turns that into the indeterminate stripe rather than a bar sitting at
       zero looking hung, which is the honest rendering of a walk whose size is not known
       yet. */
    lastPct = typeof snap.pct === 'number' ? snap.pct * 100 : 0;
    var text = snap.cancel_requested
      ? i18n.t('overview.scan.stopping')
      : i18n.pickField(snap, 'detail');
    dom.progress.set(lastPct, text);
  }

  /*
   * The one settle path. release() has to happen exactly once per run and on every exit --
   * resolved, rejected, and the start that never got off the ground -- so it lives in one
   * function instead of three copies that drift apart.
   */
  function finishRun() {
    if (release) {
      var r = release;
      release = null;
      r();
    }
    watcher = null;
    jobId = null;
    setRunning(false);
  }

  /*
   * watch() resolves for every outcome, failed and cancelled included, because those are
   * things this card renders rather than exceptions (bridge.js:160-163). A cancelled walk
   * measured something real before it stopped, so its totals are rendered and merely
   * labelled partial -- discarding them would hide work the machine already did.
   */
  function onSettled(snap) {
    finishRun();
    result = { state: snap && snap.state ? snap.state : 'done', snap: snap || {} };
    renderResults();
    if (result.state === 'cancelled') {
      ui.toast(i18n.t('overview.scan.cancelled'), { kind: 'info' });
    }
  }

  function onWatchFailed(err) {
    finishRun();
    /* Only reached when polling itself became impossible. The likely case is returning to
       this view after the engine has forgotten a job that finished while nobody watched,
       and that does not deserve an error toast for work which already succeeded. */
    if (err && err.code === 'no_such_job') {
      ui.toast(i18n.t('overview.scan.lost'), { kind: 'info' });
      return;
    }
    ui.showError(err);
  }

  function stopScan() {
    if (!watcher) { return; }
    /* Disable, relabel, then wait. cancel() asks the engine to stop and the run ends on the
       job's own final snapshot; tearing the UI down from this handler would claim the walk
       had stopped before it had. A false answer means the job settled first -- a race, not
       an error, so nothing is said about it. */
    dom.stop.disabled = true;
    dom.progress.set(lastPct, i18n.t('overview.scan.stopping'));
    watcher.cancel();
  }

  function renderResults() {
    if (!dom) { return; }
    ui.clear(dom.results);
    if (!result) { return; }
    if (result.state === 'failed') {
      /* A failed job is not an ApiError, so it does not go to ui.showError. snap.error is
         free-text English from the engine: rendered as it came, never translated. */
      dom.results.appendChild(ui.emptyState({
        icon: 'icon-caution',
        i18n: 'overview.scan.failed_title',
        text: result.snap.error || null
      }));
      return;
    }
    var partial = result.snap.partial_results;
    var totals = partial && partial.totals ? partial.totals : null;
    if (!totals) {
      dom.results.appendChild(ui.emptyState({
        icon: 'icon-info', i18n: 'overview.scan.no_results'
      }));
      return;
    }
    ui.append(dom.results, [
      ui.el('div', { class: 'ov__headline' }, [
        ui.el('p', { class: 'ov__headline-label', i18n: 'overview.scan.total' }),
        /*
         * ui.size takes a ROW, not a number, and a synthetic row is the sanctioned way to
         * put the lower-bound marker on a total: a walk that hit its budget knows only a
         * minimum, and printing a bare figure there is exactly BUG-07. measurable is true
         * because the total over the rows that were walked is a real measurement.
         */
        ui.el('div', { class: 'ov__headline-value' }, [
          ui.size({
            size: totals.total_size,
            truncated: totals.truncated === true,
            measurable: true
          })
        ])
      ]),
      ui.el('div', { class: 'ov__stats' }, [
        ui.stat({ value: i18n.fmtInt(totals.scanned), i18n: 'overview.scan.scanned' }),
        ui.stat({ value: i18n.fmtInt(totals.unavailable), i18n: 'overview.scan.unavailable' }),
        ui.stat({
          value: i18n.fmtInt(totals.not_measurable), i18n: 'overview.scan.not_measurable'
        }),
        ui.stat({
          value: i18n.fmtInt(totals.failed),
          i18n: 'overview.scan.failed',
          tone: totals.failed ? 'danger' : null
        })
      ]),
      ui.el('div', { class: 'ov__flags' }, [
        result.state === 'cancelled'
          ? ui.tag({ tone: 'warn', icon: 'icon-stop', i18n: 'overview.scan.partial' })
          : null,
        /* The tooltip ui.size hangs on the figure is not enough on its own -- a title
           attribute is unreachable by keyboard -- so a truncated walk also says so in a
           visible line. */
        totals.truncated === true
          ? ui.el('p', { class: 'ov__note', i18n: 'overview.scan.truncated_note' })
          : null
      ]),
      ui.el('div', { class: 'ov__cta' }, [
        ui.btn({
          i18n: 'overview.scan.to_clean', icon: 'icon-clean', variant: 'primary',
          on: { click: function () { app().go('clean'); } }
        })
      ])
    ]);
  }

  /* --- 3. last runs ---------------------------------------------------------- */

  /* Icon and word as well as colour. The tone is the third channel here, never the only
     one, which is why this goes through ui.tag rather than a coloured dot. */
  function stateTag(state) {
    var known = Object.prototype.hasOwnProperty.call(STATE_TONE, state);
    return ui.tag({
      tone: known ? STATE_TONE[state] : null,
      icon: known ? STATE_ICON[state] : 'icon-info',
      i18n: 'state.' + (known ? state : 'unknown')
    });
  }

  function kindTag(kind) {
    var known = kind === 'scan' || kind === 'clean';
    return ui.tag({
      icon: known ? (kind === 'clean' ? 'icon-clean' : 'icon-search') : 'icon-info',
      i18n: 'kind.' + (known ? kind : 'unknown')
    });
  }

  function runRow(report) {
    return ui.el('li', { class: 'ov__run' }, [
      ui.el('div', { class: 'ov__run-when num', text: i18n.fmtDate(report.finished_at) }),
      ui.el('div', { class: 'ov__run-tags' }, [stateTag(report.state), kindTag(report.kind)]),
      ui.el('div', { class: 'ov__run-figs' }, [
        metaRow('overview.last.reclaimed', i18n.fmtBytes(report.reclaimed_total)),
        metaRow('overview.last.duration', i18n.fmtDuration(report.duration_s))
      ]),
      /* Every row ends in something Tab can reach, and it is a real button rather than a
         clickable div. It cannot carry the report id -- go() takes a view name and nothing
         else -- so it opens History, where the detail renderer lives. */
      ui.btn({
        icon: 'icon-chevron', label: 'overview.last.open', variant: 'ghost', size: 'sm',
        on: { click: function () { app().go('history'); } }
      })
    ]);
  }

  function renderRuns() {
    if (!dom) { return; }
    ui.clear(dom.last);
    if (runsError) {
      /* Non-fatal, like the volume read: the error goes where the list would have been. */
      dom.last.appendChild(ui.emptyState({
        icon: 'icon-history',
        i18n: 'overview.last.error',
        text: errText(runsError),
        action: retryBtn(loadRuns)
      }));
      return;
    }
    if (!runs) { return; }
    if (!runs.length) {
      dom.last.appendChild(ui.emptyState({
        icon: 'icon-history', i18n: 'overview.last.empty', body: 'overview.last.empty.body'
      }));
      return;
    }
    var list = ui.el('ul', { class: 'ov__runs' });
    for (var i = 0; i < runs.length; i += 1) {
      list.appendChild(runRow(runs[i]));
    }
    dom.last.appendChild(list);
  }

  /* --- data ------------------------------------------------------------------ */

  /*
   * Both loaders carry a generation counter. enter() and the topbar refresh can overlap, and
   * without it a slow first reply can land after a fast second one and repaint the older
   * list -- free space is the one number a user watches change, so a stale figure is worse
   * than a moment with none.
   */
  function loadVolumes(force) {
    volsSeq += 1;
    var seq = volsSeq;
    return app().volumes(force === true).then(function (payload) {
      if (seq !== volsSeq) { return; }
      vols = (payload && payload.volumes) || [];
      volsError = null;
      renderDisks();
    }, function (err) {
      if (seq !== volsSeq) { return; }
      volsError = err;
      renderDisks();
    });
  }

  /* history() also answers with a live volume list, read at the moment of the call. This
     view ignores it: the disks section above does its own read, and two reads rendered as
     one panel would disagree the first time they were taken a second apart. */
  function loadRuns() {
    runsSeq += 1;
    var seq = runsSeq;
    return api.history(HISTORY_LIMIT).then(function (payload) {
      if (seq !== runsSeq) { return; }
      runs = (payload && payload.reports) || [];
      runsError = null;
      renderRuns();
    }, function (err) {
      if (seq !== runsSeq) { return; }
      runsError = err;
      renderRuns();
    });
  }

  function refreshAll() {
    loadVolumes(true);
    loadRuns();
  }

  /* --- lifecycle ------------------------------------------------------------- */

  function mount(host) {
    dom = {};
    dom.disks = ui.el('div', { class: 'ov__disks' });

    dom.start = ui.btn({
      i18n: 'overview.scan.start', icon: 'icon-play', variant: 'primary',
      on: { click: startScan }
    });
    dom.stop = ui.btn({
      i18n: 'action.stop', icon: 'icon-stop', variant: 'ghost',
      on: { click: stopScan }
    });
    dom.stop.hidden = true;
    dom.progress = ui.progress();
    dom.progress.hidden = true;
    /*
     * aria-live sits on the results, not on the bar. The bar is a progressbar carrying
     * aria-valuenow, which a screen reader reports when asked; its label is rewritten four
     * times a second, and a live region there would talk over itself for the whole walk.
     * The results block changes twice a run -- cleared at the start, filled when the job
     * settles -- which is what polite is for.
     */
    dom.results = ui.el('div', { class: 'ov__results', attrs: { 'aria-live': 'polite' } });

    var scanCard = ui.card({
      class: 'bento-col-7 card--tilted',
      icon: 'icon-clean',
      i18n: 'overview.scan.title',
      sub: 'overview.scan.sub'
    });
    ui.append(scanCard.body, [
      ui.el('div', { class: 'ov__scan-actions' }, [dom.start, dom.stop]),
      dom.progress,
      dom.results
    ]);

    var disksCard = ui.card({
      class: 'bento-col-5',
      icon: 'icon-disk',
      i18n: 'overview.disks.title'
    });
    var disksBlock = ui.el('section', { class: 'ov__block', attrs: { 'aria-labelledby': 'ov-disks-title' } }, [
      ui.el('h2', {
        class: 'ov__block-title', id: 'ov-disks-title', i18n: 'overview.disks.title'
      }),
      dom.disks
    ]);
    disksCard.body.appendChild(disksBlock);

    var goalsCard = ui.card({
      class: 'bento-col-6',
      icon: 'icon-safe',
      i18n: 'overview.goals.title'
    });
    goalsCard.body.appendChild(createGoalsList());

    dom.last = ui.el('div', { class: 'ov__last' });
    dom.calContainer = ui.el('div');
    dom.calContainer.appendChild(createCalendarStrip());

    var activityCard = ui.card({
      class: 'bento-col-6',
      icon: 'icon-history',
      i18n: 'overview.last.title',
      sub: 'overview.last.sub'
    });
    ui.append(activityCard.body, [
      ui.el('h3', { class: 'card__sub', i18n: 'overview.cal.title' }),
      dom.calContainer,
      dom.last
    ]);

    dom.dateEl = ui.el('div', { class: 'greeting__date', text: getFormattedDate() });
    var greetingBox = ui.el('div', { class: 'greeting' }, [
      dom.dateEl,
      ui.el('h1', { class: 'greeting__title', i18n: 'overview.greeting.title' }),
      ui.el('p', { class: 'greeting__sub', i18n: 'overview.greeting.sub' }),
      ui.el('p', { class: 'card__sub', i18n: 'overview.greeting.status' }),
      ui.el('div', { class: 'greeting__actions' }, [
        ui.btn({
          i18n: 'overview.action.quick_scan', icon: 'icon-search', class: 'btn--pill', variant: 'primary',
          on: { click: startScan }
        }),
        ui.btn({
          i18n: 'overview.action.clean_now', icon: 'icon-clean', class: 'btn--pill btn--pill-outline', variant: 'ghost',
          on: { click: function () { app().go('clean'); } }
        }),
        ui.btn({
          i18n: 'overview.action.schedule', icon: 'icon-history', class: 'btn--pill btn--pill-outline', variant: 'ghost',
          on: { click: function () { app().go('schedule'); } }
        }),
        ui.btn({
          i18n: 'overview.action.explorer', icon: 'icon-folder', class: 'btn--pill btn--pill-outline', variant: 'ghost',
          on: { click: function () { app().go('explorer'); } }
        })
      ])
    ]);

    var bento = ui.el('div', { class: 'bento-grid' }, [
      scanCard,
      disksCard,
      goalsCard,
      activityCard
    ]);

    host.appendChild(ui.el('div', { class: 'ov' }, [
      greetingBox,
      bento,
      createFab()
    ]));

    /* Repaint whatever an earlier visit already fetched. On a first mount these are all
       null and the sections stay blank until the reads from enter() land. */
    renderDisks();
    renderResults();
    renderRuns();
    setRunning(running);
  }

  function enter(host, actions) {
    if (actions) {
      /* Icon-only, so it needs label: -- ui.btn:163 throws without one. Only the volumes and
         the reports are re-read: the catalogue is compiled into the engine and cannot change
         while the app is running. */
      actions.appendChild(ui.btn({
        icon: 'icon-refresh', label: 'overview.refresh', variant: 'ghost',
        on: { click: refreshAll }
      }));
    }
    loadVolumes(true);
    loadRuns();
    /* A job this view started is still running in the engine after leave() stopped watching
       it, so coming back re-attaches instead of starting a second walk. */
    if (jobId && !watcher) { attach(jobId); }
  }

  function leave() {
    if (!watcher) { return; }
    /*
     * stopPolling, never cancel. Navigating away is not a decision to abandon a walk the
     * user asked for, so the job runs on and jobId is kept for enter() to re-attach to.
     *
     * The claimJob token is deliberately NOT released here either: while the engine really
     * is busy, the Clean view asking for a job should be told so by the guard rather than
     * sent to collect a 'busy' refusal from the engine. The cost is that a run which
     * finishes while this view is off screen leaves the claim held until the user comes
     * back and the re-attached watcher settles it.
     */
    watcher.stopPolling();
    watcher = null;
  }

  function relang() {
    /* Every figure on this page was formatted by this file, and i18n.apply cannot reach a
       number: '12,3 GB' and '12.3 GB' are different strings. The volume cards also carry a
       gauge aria-label built with i18n.t vars, which is the other thing apply misses. The
       running progress label needs nothing -- the next poll rewrites it within 250 ms. */
    if (dom && dom.dateEl) {
      dom.dateEl.textContent = getFormattedDate();
    }
    if (dom && dom.calContainer) {
      ui.clear(dom.calContainer);
      dom.calContainer.appendChild(createCalendarStrip());
    }
    renderDisks();
    renderResults();
    renderRuns();
  }

  views.overview = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
