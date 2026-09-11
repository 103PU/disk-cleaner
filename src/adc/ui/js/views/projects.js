/*
 * views/projects.js -- the Project Sweeper (docs/02-SPEC.md 6.3).
 *
 * Scans a user-chosen root folder for orphan build junk, virtualenvs, and caches.
 * Enforces two-stage preview -> confirm -> execute flow with single-use tokens.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;

  function app() { return ADC.app; }

  /* Named phases */
  var IDLE = 0;
  var SCANNING = 1;
  var PLANNING = 2;
  var DELETING = 3;
  var SUMMARY = 4;

  var DASH = '—';

  /* The 5 categories matching sweeper.py CATEGORIES */
  var ALL_CATEGORIES = ['node_modules', 'venv', 'build', 'cache', 'framework'];

  var CAT_CLASSES = {
    node_modules: 'ps__cat ps__cat--node_modules',
    venv: 'ps__cat ps__cat--venv',
    build: 'ps__cat ps__cat--build',
    cache: 'ps__cat ps__cat--cache',
    framework: 'ps__cat ps__cat--framework'
  };

  var COLS = [
    { key: 'check', label: null, sortable: false, numeric: false },
    { key: 'name', label: 'projects.col.name', sortable: true, numeric: false },
    { key: 'project', label: 'projects.col.project', sortable: true, numeric: false },
    { key: 'category', label: 'projects.col.category', sortable: true, numeric: false },
    { key: 'size', label: 'projects.col.size', sortable: true, numeric: true },
    { key: 'idle', label: 'projects.col.idle', sortable: true, numeric: true },
    { key: 'actions', label: 'projects.col.actions', sortable: false, numeric: false }
  ];

  var SORTS = {
    name: function (a, b) { return String(a.name).localeCompare(String(b.name)); },
    project: function (a, b) { return String(a.project_name || a.project).localeCompare(String(b.project_name || b.project)); },
    category: function (a, b) { return String(a.category).localeCompare(String(b.category)); },
    size: function (a, b) { return (a.size || 0) - (b.size || 0); },
    idle: function (a, b) { return (a.idle_days || 0) - (b.idle_days || 0); }
  };

  /* --- state ------------------------------------------------------------------ */

  var dom = null;
  var phase = IDLE;

  var head = null;
  var level = null;
  var levelSeq = 0;
  var outcome = null;
  var plan = null;
  var lastPct = 0;

  var jobId = null;
  var watcher = null;
  var release = null;
  var logPane = null;

  var selectedIds = {};
  var sortKey = 'size';
  var sortAsc = false;
  var visibleCats = {};
  var filterText = '';

  /* --- helpers ---------------------------------------------------------------- */

  function noteRow(iconName, child) {
    return ui.el('p', { class: 'ps__note' }, [ui.icon(iconName, 'ps__note-ico'), child]);
  }

  function note(iconName, text) {
    return noteRow(iconName, ui.el('span', { text: text }));
  }

  /* --- lifecycle -------------------------------------------------------------- */

  function mount(host) {
    var rootInput = ui.el('input', {
      type: 'text', class: 'ps__root', id: 'ps-root',
      attrs: { placeholder: i18n.t('projects.root.hint'), title: i18n.t('projects.root.hint') }
    });
    var rootBox = ui.el('div', { class: 'ps__root-box' }, [
      ui.field({ i18n: 'projects.root', control: rootInput, id: 'ps-root' })
    ]);

    var ageInput = ui.el('input', {
      type: 'number', class: 'ps__age', id: 'ps-age',
      attrs: { min: 0, placeholder: i18n.t('projects.age.hint'), title: i18n.t('projects.age.hint') }
    });
    var ageUnit = ui.el('span', { class: 'ps__age-unit', i18n: 'projects.age.unit' });
    var ageWrap = ui.el('div', { class: 'ps__age-box' }, [ageInput, ageUnit]);
    var ageBox = ui.el('div', null, [
      ui.field({ i18n: 'projects.age', control: ageWrap, id: 'ps-age' })
    ]);

    var scanBtn = ui.btn({ i18n: 'projects.scan', variant: 'primary', on: { click: startScan } });
    var stopBtn = ui.btn({ i18n: 'projects.stop', variant: 'ghost', disabled: true, on: { click: stopRun } });
    var refreshBtn = ui.btn({ i18n: 'projects.refresh', variant: 'ghost', disabled: true, on: { click: startScan } });

    var controls = ui.el('div', { class: 'ps__controls' }, [
      rootBox,
      ageBox,
      scanBtn,
      stopBtn,
      refreshBtn
    ]);

    var progress = ui.progress();
    progress.hidden = true;

    var totals = ui.el('div', { class: 'ps__totals' });
    var catBar = ui.el('div', { class: 'ps__cat-bar' });

    var selAll = ui.btn({ i18n: 'projects.select_all', variant: 'ghost', size: 'sm', on: { click: selectAll } });
    var selNone = ui.btn({ i18n: 'projects.select_none', variant: 'ghost', size: 'sm', on: { click: selectNone } });
    var selectedCount = ui.el('span', { class: 'ps__count' });

    var filter = ui.el('input', {
      type: 'text', class: 'ps__filter',
      attrs: { placeholder: i18n.t('projects.filter'), title: i18n.t('projects.filter.hint'), 'aria-label': i18n.t('projects.filter') },
      on: { input: onFilter }
    });
    var filterBox = ui.el('div', { class: 'ps__filter-box' }, [filter]);
    var showingCount = ui.el('span', { class: 'ps__count' });

    var tools = ui.el('div', { class: 'ps__tools' }, [
      ui.el('div', null, [selAll, selNone, selectedCount]),
      ui.el('div', null, [filterBox, showingCount])
    ]);

    var theadTr = ui.el('tr');
    for (var c = 0; c < COLS.length; c++) {
      theadTr.appendChild(headCell(COLS[c]));
    }
    var tbody = ui.el('tbody');
    var caption = ui.el('caption', { class: 'ps__caption', i18n: 'projects.pick' });
    var table = ui.el('table', { class: 'ps__table' }, [
      caption,
      ui.el('thead', null, [theadTr]),
      tbody
    ]);

    var tableWrap = ui.el('div', { class: 'ps__table-wrap' }, [table]);
    tableWrap.hidden = true;

    var tableEmpty = ui.el('div', { class: 'ps__table-empty' });
    tableEmpty.appendChild(ui.emptyState({
      icon: 'icon-search', i18n: 'projects.empty', body: 'projects.empty.body'
    }));
    tableEmpty.hidden = true;

    var notes = ui.el('div', { class: 'ps__notes' });

    var previewBtn = ui.btn({
      i18n: 'projects.preview', variant: 'primary', disabled: true, on: { click: previewClean }
    });
    previewBtn.hidden = true;

    var empty = ui.emptyState({
      icon: 'icon-projects', i18n: 'projects.pick', body: 'projects.pick.help'
    });

    logPane = ui.consolePane();

    var body = ui.el('div', { class: 'ps__body' }, [
      controls,
      progress,
      totals,
      catBar,
      tools,
      tableWrap,
      tableEmpty,
      empty,
      notes,
      previewBtn,
      logPane
    ]);

    host.appendChild(body);

    dom = {
      root: rootInput,
      age: ageInput,
      scanBtn: scanBtn,
      stopBtn: stopBtn,
      refreshBtn: refreshBtn,
      progress: progress,
      totals: totals,
      catBar: catBar,
      selectedCount: selectedCount,
      showingCount: showingCount,
      filter: filter,
      theadTr: theadTr,
      tbody: tbody,
      tableWrap: tableWrap,
      tableEmpty: tableEmpty,
      empty: empty,
      notes: notes,
      previewBtn: previewBtn
    };

    ALL_CATEGORIES.forEach(function (cat) { visibleCats[cat] = true; });
  }

  function enter(host, actions) {
    if (actions) {
      actions.appendChild(ui.btn({
        icon: 'icon-refresh', label: 'projects.refresh', variant: 'ghost',
        on: { click: function () { startScan(); } }
      }));
    }

    if (!dom.root.value) {
      api.sweepDefaults().then(function (res) {
        if (dom && !dom.root.value) {
          dom.root.value = res.root || '';
          dom.age.value = res.min_age_days != null ? res.min_age_days : 30;
        }
      }).catch(ui.showError);
    }

    if (jobId && !watcher && (phase === SCANNING || phase === DELETING)) {
      attach(jobId);
    }

    renderAll();
  }

  function leave() {
    if (watcher) {
      watcher.stopPolling();
      watcher = null;
    }
  }

  function relang() {
    renderAll();
  }

  function finishRun() {
    if (release) {
      var done = release;
      release = null;
      done();
    }
    watcher = null;
    if (phase === SCANNING) phase = IDLE;
    if (phase === DELETING) phase = SUMMARY;
  }

  /* --- core workflow ---------------------------------------------------------- */

  function startScan() {
    var root = dom.root.value.trim();
    if (!root) {
      ui.toast(i18n.t('projects.root.empty'), { kind: 'warn' });
      return;
    }
    var age = parseInt(dom.age.value, 10);
    if (isNaN(age) || age < 0) age = 0;

    var claim = app().claimJob('sweep');
    if (!claim) {
      var busy = app().busyWith();
      ui.toast(i18n.t('projects.busy', { kind: busy || '' }), { kind: 'warn' });
      return;
    }
    release = claim;

    var seq = ++levelSeq;
    phase = SCANNING;
    level = null;
    head = null;
    outcome = null;
    selectedIds = {};
    plan = null;
    lastPct = 0;

    if (logPane) {
      logPane.clear();
      logPane.setBusy(true);
    }

    dom.progress.set(0, i18n.t('projects.starting'));
    renderAll();

    api.sweepStart(root, age).then(function (started) {
      if (seq !== levelSeq) return;
      head = started;
      attach(started.job_id);
    }, function (err) {
      if (seq !== levelSeq) return;
      finishRun();
      renderAll();
      ui.showError(err);
    });
  }

  function attach(id) {
    jobId = id;
    watcher = api.watch(id, { onSnapshot: onSnapshot, onEvents: onEvents });
    watcher.promise.then(onSettled, onWatchFailed);
  }

  function onSnapshot(snap) {
    if (!dom) return;
    lastPct = typeof snap.pct === 'number' ? snap.pct * 100 : 0;
    var msg = snap.cancel_requested
      ? i18n.t('projects.stopping')
      : i18n.pickField(snap, 'detail');
    dom.progress.set(lastPct, msg);

    var partial = snap.partial_results;
    if (partial && partial.kind === 'sweep') {
      level = partial;
    }
    renderAll();
  }

  function onEvents(events) {
    if (logPane) logPane.push(events);
  }

  function onSettled(snap) {
    finishRun();
    var final = snap || {};
    if (final.partial_results && final.partial_results.kind === 'sweep') {
      level = final.partial_results;
    }
    outcome = final.state === 'cancelled' || final.state === 'failed' ? final.state : null;
    if (logPane) logPane.setBusy(false);
    renderAll();

    if (outcome === 'cancelled') {
      ui.toast(i18n.t('projects.cancelled'), { kind: 'info' });
    } else if (outcome === 'failed') {
      ui.toast(i18n.t('projects.failed'), { kind: 'error' });
    } else if (phase === IDLE) {
      var total = (level && level.totals && level.totals.total_size) || 0;
      ui.toast(i18n.t('projects.scan_done', { size: i18n.fmtBytes(total) }), { kind: 'success' });
    } else if (phase === SUMMARY) {
      ui.toast(i18n.t('projects.done'), { kind: 'success' });
    }
  }

  function onWatchFailed(err) {
    finishRun();
    if (logPane) logPane.setBusy(false);
    renderAll();
    if (err && err.code === 'no_such_job') {
      ui.toast(i18n.t('projects.lost'), { kind: 'info' });
      return;
    }
    ui.showError(err);
  }

  function stopRun() {
    if (!watcher) return;
    dom.stopBtn.disabled = true;
    dom.progress.set(lastPct, i18n.t('projects.stopping'));
    watcher.cancel();
  }

  /* --- preview & delete ------------------------------------------------------- */

  function previewClean() {
    var ids = Object.keys(selectedIds);
    if (!ids.length) {
      ui.toast(i18n.t('projects.nothing_selected'), { kind: 'warn' });
      return;
    }
    var claim = app().claimJob('sweep');
    if (!claim) {
      var busy = app().busyWith();
      ui.toast(i18n.t('projects.busy', { kind: busy || '' }), { kind: 'warn' });
      return;
    }

    dom.previewBtn.disabled = true;
    var oldText = dom.previewBtn.textContent;
    dom.previewBtn.textContent = i18n.t('projects.previewing');

    api.sweepPlan(jobId, ids).then(function (res) {
      dom.previewBtn.textContent = oldText;
      dom.previewBtn.disabled = false;
      plan = res;
      showConfirm();
    }, function (err) {
      if (claim) claim();
      dom.previewBtn.textContent = oldText;
      dom.previewBtn.disabled = false;
      ui.showError(err);
    });
  }

  function showConfirm() {
    if (!plan) return;
    if (!plan.count) {
      ui.toast(i18n.t('projects.nothing_to_delete'), { kind: 'warn' });
      return;
    }

    var lines = [
      i18n.t('projects.confirm.rows', { n: i18n.fmtInt(plan.count) }),
      i18n.t('projects.confirm.total', { size: i18n.fmtBytes(plan.est_total) })
    ];
    if (plan.skipped) {
      lines.push(i18n.t('projects.confirm.skipped', { n: i18n.fmtInt(plan.skipped) }));
    }
    if (plan.has_irreversible) {
      lines.push(i18n.t('projects.confirm.irreversible'));
    }

    ui.confirm({
      i18n: 'projects.confirm.title',
      lines: lines,
      danger: true,
      confirmKey: 'projects.confirm.ok'
    }).then(function (ok) {
      if (!ok) {
        var cl = app().claimJob('sweep');
        if (cl) cl();
        return;
      }
      executeClean();
    });
  }

  function executeClean() {
    if (!plan) return;
    phase = DELETING;
    release = app().claimJob('sweep') || release;
    if (logPane) {
      logPane.clear();
      logPane.setBusy(true);
    }
    dom.progress.set(0, i18n.t('projects.deleting'));
    renderAll();

    api.sweepExecute(plan.token).then(function (res) {
      attach(res.job_id);
    }, function (err) {
      finishRun();
      renderAll();
      ui.showError(err);
    });
  }

  /* --- rendering -------------------------------------------------------------- */

  function renderAll() {
    if (!dom) return;

    var running = phase === SCANNING || phase === DELETING;
    dom.root.disabled = running;
    dom.age.disabled = running;
    dom.scanBtn.disabled = running;
    dom.refreshBtn.disabled = running || !head;
    dom.stopBtn.disabled = !running;
    dom.progress.hidden = !running;

    var hasLevel = !!level;
    var rows = visibleRows();
    var hasRows = rows.length > 0;

    dom.empty.hidden = hasLevel;
    dom.tableWrap.hidden = !hasLevel || !hasRows;
    dom.tableEmpty.hidden = !hasLevel || hasRows;

    renderStats();
    renderCats();
    renderTable();
    renderNotes();

    var selCount = Object.keys(selectedIds).length;
    dom.selectedCount.textContent = selCount
      ? i18n.tn('projects.selected', selCount, { n: i18n.fmtInt(selCount) })
      : '';

    var allCount = (level && level.rows && level.rows.length) || 0;
    dom.showingCount.textContent = hasLevel
      ? i18n.t('projects.showing', { shown: i18n.fmtInt(rows.length), all: i18n.fmtInt(allCount) })
      : '';

    dom.previewBtn.disabled = running || selCount === 0;
    dom.previewBtn.hidden = !hasLevel || phase === SUMMARY || phase === DELETING;
  }

  function renderStats() {
    ui.clear(dom.totals);
    var t = level && level.totals;
    if (!t) return;
    var lower = t.truncated === true;

    ui.append(dom.totals, [
      ui.stat({ value: i18n.fmtInt(t.found), i18n: 'projects.stat.found' }),
      ui.stat({ value: i18n.fmtBytes(t.total_size, { truncated: lower }), i18n: 'projects.stat.size' }),
      ui.stat({ value: i18n.fmtInt(t.active_projects), i18n: 'projects.stat.active' }),
      ui.stat({ value: i18n.fmtInt(t.scanned_dirs), i18n: 'projects.stat.dirs' })
    ]);
  }

  function renderCats() {
    ui.clear(dom.catBar);
    var t = level && level.totals && level.totals.by_category;
    if (!t) return;

    ALL_CATEGORIES.forEach(function (cat) {
      var stats = t[cat];
      if (!stats) return;
      var isActive = visibleCats[cat];

      var btn = ui.el('button', {
        class: isActive ? 'ps__cat-btn is-active' : 'ps__cat-btn',
        type: 'button',
        on: {
          click: function () {
            visibleCats[cat] = !visibleCats[cat];
            renderAll();
          }
        }
      }, [
        ui.el('span', { i18n: 'projects.cat.' + cat }),
        document.createTextNode(' (' + i18n.fmtInt(stats.count) + ' · ' + i18n.fmtBytes(stats.size) + ')')
      ]);
      dom.catBar.appendChild(btn);
    });
  }

  function onFilter() {
    filterText = dom.filter.value.trim().toLowerCase();
    renderTable();
  }

  function selectAll() {
    var rows = visibleRows();
    rows.forEach(function (r) { if (!r.always_safe) selectedIds[r.node_id] = true; });
    renderAll();
  }

  function selectNone() {
    selectedIds = {};
    renderAll();
  }

  function headCell(col) {
    if (col.key === 'check') {
      return ui.el('th', { class: 'ps__th ps__th--check', attrs: { scope: 'col' } });
    }
    var active = col.sortable && sortKey === col.key;
    var classes = ['ps__th'];
    if (col.numeric) classes.push('ps__th--num');
    if (active) classes.push('ps__th--sorted');

    var cell = ui.el('th', { class: classes.join(' '), attrs: { scope: 'col' } });
    if (!col.sortable) {
      cell.appendChild(ui.el('span', { i18n: col.label }));
      return cell;
    }

    cell.setAttribute('aria-sort', active ? (sortAsc ? 'ascending' : 'descending') : 'none');
    var btn = ui.el('button', {
      class: 'ps__sort', type: 'button',
      attrs: { 'aria-label': i18n.t(col.label) },
      on: {
        click: function () {
          if (sortKey === col.key) sortAsc = !sortAsc;
          else { sortKey = col.key; sortAsc = !col.numeric; }
          renderTable();
        }
      }
    }, [ui.el('span', { i18n: col.label })]);

    if (active && sortAsc) btn.appendChild(ui.icon('icon-chevron', 'ps__arrow ps__arrow--up'));
    if (active && !sortAsc) btn.appendChild(ui.icon('icon-chevron', 'ps__arrow ps__arrow--down'));

    cell.appendChild(btn);
    return cell;
  }

  function visibleRows() {
    var rows = (level && level.rows) || [];
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!visibleCats[r.category]) continue;
      if (filterText) {
        var n = String(r.name).toLowerCase();
        var p = String(r.project_name || r.project).toLowerCase();
        if (n.indexOf(filterText) === -1 && p.indexOf(filterText) === -1) continue;
      }
      out.push(r);
    }
    var cmp = SORTS[sortKey] || SORTS.size;
    out.sort(function (a, b) {
      var delta = cmp(a, b);
      return sortAsc ? delta : -delta;
    });
    return out;
  }

  function renderTable() {
    if (!dom) return;
    ui.clear(dom.tbody);
    var rows = visibleRows();
    var running = phase === SCANNING || phase === DELETING;

    var activeId = document.activeElement ? document.activeElement.id : null;

    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var id = 'ps-chk-' + r.node_id;

      var box = ui.el('input', {
        type: 'checkbox', id: id,
        checked: !!selectedIds[r.node_id],
        disabled: running || r.always_safe,
        on: {
          change: (function (nid) {
            return function (e) {
              if (e.target.checked) selectedIds[nid] = true;
              else delete selectedIds[nid];
              renderAll();
            };
          })(r.node_id)
        }
      });

      var catCls = CAT_CLASSES[r.category] || 'ps__cat';
      var catBadge = ui.el('span', { class: catCls, i18n: 'projects.cat.' + r.category });

      var idleText = r.idle_days != null
        ? i18n.t('projects.idle_days', { n: i18n.fmtInt(r.idle_days) })
        : DASH;

      var tr = ui.el('tr', { class: 'ps__tr' }, [
        ui.el('td', { class: 'ps__td ps__td--check' }, [box]),
        ui.el('td', { class: 'ps__td ps__td--name' }, [
          ui.el('label', { attrs: { for: id }, text: r.name }),
          r.always_safe ? ui.el('span', { class: 'ps__safe-tag', text: 'cache' }) : null,
          ui.el('span', { class: 'ps__project-path', text: r.path })
        ]),
        ui.el('td', { class: 'ps__td', text: r.project_name || r.project }),
        ui.el('td', { class: 'ps__td' }, [catBadge]),
        ui.el('td', { class: 'ps__td num' }, [ui.size(r)]),
        ui.el('td', { class: 'ps__td num' }, [
          ui.el('span', { class: 'ps__idle', text: idleText })
        ]),
        ui.el('td', { class: 'ps__td ps__td--act' }, [
          ui.btn({
            icon: 'icon-explorer', label: 'projects.reveal', variant: 'ghost', size: 'sm',
            disabled: running,
            on: {
              click: (function (nid) {
                return function () {
                  api.sweepReveal(nid).then(function (res) {
                    ui.toast(i18n.t('projects.reveal.done'), { kind: 'success' });
                  }).catch(ui.showError);
                };
              })(r.node_id)
            }
          })
        ])
      ]);
      dom.tbody.appendChild(tr);
    }

    if (activeId) {
      var el = document.getElementById(activeId);
      if (el) el.focus();
    }
  }

  function renderNotes() {
    ui.clear(dom.notes);
    var t = level && level.totals;
    if (!t) return;

    if (t.truncated) {
      dom.notes.appendChild(note('icon-caution', i18n.t('projects.note.truncated')));
    }
    if (t.denied_count > 0) {
      dom.notes.appendChild(note('icon-lock', i18n.t('projects.note.denied', { n: i18n.fmtInt(t.denied_count) })));
    }
    if (t.unproven > 0) {
      dom.notes.appendChild(note('icon-info', i18n.t('projects.note.unproven', { n: i18n.fmtInt(t.unproven) })));
    }
    if (t.active_projects > 0) {
      dom.notes.appendChild(note('icon-shield', i18n.t('projects.note.active', { n: i18n.fmtInt(t.active_projects) })));
    }
    if (outcome === 'cancelled') {
      dom.notes.appendChild(note('icon-stop', i18n.t('projects.note.cancelled')));
    }
  }

  views.projects = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
