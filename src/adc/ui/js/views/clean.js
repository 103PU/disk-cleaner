/*
 * views/clean.js -- ADC.views.clean. The one view that deletes anything.
 *
 * It owns a single loop, and the loop is the app:
 *
 *   pick targets -> scan them for real sizes -> preview the delete (the engine mints a
 *   priced plan) -> confirm against those exact figures -> redeem the token -> read the
 *   receipt.
 *
 * Three things in that sentence are spec rows rather than taste.
 *
 *   The confirmation states the row count and the byte total (docs/02-SPEC.md 4.6). A
 *   dialog that asks "are you sure?" and nothing else confirms nothing, and v1's did
 *   exactly that.
 *
 *   The dangerous rows go through a second, separate handshake. The first preview
 *   deliberately SKIPS them and only names them in plan.dangerous_ids, so this page has
 *   to ask again and re-price before any of them can be included. And nothing about what
 *   gets deleted travels with clean_execute -- it takes the token and nothing else -- so
 *   a page cannot widen a clean after the figure was approved.
 *
 *   The scan table grows while the walk runs (docs/02-SPEC.md 3.1). partial_results is
 *   read live off the running walk, so a size cell fills the moment its target is
 *   measured instead of everything appearing at the end.
 *
 * What this file deliberately does NOT do:
 *   - format a byte figure that came off a scan row or a plan by hand. Those numbers
 *     carry honesty flags (measurable false, truncated) and go through ui.size(ROW) so
 *     the flags travel with the value.
 *   - hold or send a path. The selection is target ids; reveal takes an id. The
 *     catalogue decides what an id means on this machine.
 *   - keep polling after the user navigates away. leave() stops this page watching; it
 *     never cancels the user's job, and enter() re-attaches.
 *   - re-implement a row, a card, a badge, a bar, a console, a toast or a dialog. Those
 *     are ui.js, and each of them carries a spec row this file would forget.
 *   - list sample_paths in the confirmation. A plan carries up to eight per item, and a
 *     confirmation that turns into a file listing stops being read.
 *   - show a target's denied_count in its row. The console already reports every refused
 *     directory as a warn line, and the summary counts them per target.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};

  /* i18n, ui and api are all loaded before this file (index.html:296-302), so they can be
     captured here the way ui.js captures i18n. ADC.app is NOT: app.js loads after the
     views because it reads this registry, so every reference to it below is late-bound. */
  var i18n = ADC.i18n;
  var ui = ADC.ui;
  var api = ADC.api;
  /* --- the state machine ------------------------------------------------------- */

  /* Five states, in one place. Every button's enabled-ness, the progress bar's
     visibility, the console spinner and whether the checkboxes are frozen are derived
     from this and from nothing else -- which is the only way a view with two chained
     bridge calls and a modal in the middle stays debuggable. */
  var IDLE = 'idle';
  var SCANNING = 'scanning';
  var PLANNING = 'planning';   /* clean_plan is in flight, or one of its dialogs is open */
  var CLEANING = 'cleaning';
  var SUMMARY = 'summary';     /* idle, with the last receipt still on screen */

  /* A stale plan is the expected case, not an exception: the token lives 600 seconds and
     is single-use, and the user just spent some of that reading a dialog. These three
     codes mean "re-price and ask again", once. */
  var STALE_PLAN = { plan_expired: true, plan_spent: true, plan_unknown: true };

  /* The report file is written a few lines AFTER the job is marked terminal, so the first
     report_detail can lose that race by a few hundred milliseconds. One retry, then the
     snapshot-only summary stands. */
  var REPORT_RETRY_MS = 700;

  var state = {
    phase: IDLE,
    catalog: null,
    catalogPending: false,
    rows: [],            /* one entry per target, in catalogue order */
    byId: {},            /* target id -> entry */
    groups: [],          /* one per category, in first-appearance order */
    /* The selection is an object used as a set. Not an array with indexOf: applying a
       preset touches 22 of 49 rows and every row then asks "am I in it?", which is
       quadratic over an array and free over a hash. */
    sel: {},
    scan: {},            /* target id -> latest scan row out of partial_results */
    seeded: false,       /* the default preset is applied once, never re-applied */
    job: null,           /* {id, kind} -- survives leave() so enter() can re-attach */
    watcher: null,       /* the live api.watch handle, or null when not watching */
    release: null,       /* ADC.app.claimJob() closure, held for as long as the job runs */
    planRetried: false,  /* guards the automatic re-preview so it cannot loop */
    live: null,          /* last snapshot of a running job, for relang() */
    snap: null,          /* final snapshot of the last clean, for the summary */
    report: null         /* its report_detail payload, once that arrives */
  };

  var dom = {
    root: null, presets: null, presetBtns: {}, summary: null, cats: null,
    count: null, totalSlot: null, progress: null, progressWrap: null,
    scanBtn: null, previewBtn: null, stopBtn: null, refreshBtn: null, log: null
  };
  /* --- small shared helpers ----------------------------------------------------- */

  function working() {
    return state.phase === SCANNING || state.phase === CLEANING || state.phase === PLANNING;
  }

  /* Where a run falls back to when it settles or is abandoned: keep the receipt on screen
     if there is one, otherwise plain idle. */
  function restPhase() {
    return state.snap ? SUMMARY : IDLE;
  }

  /* ADC.app.settings() is null until the first load resolves, and everything below treats
     "unknown" as the cautious answer rather than as a default value. */
  function settings() {
    return ADC.app.settings();
  }

  /*
   * "Size on disk" changes what a size cell MEANS, so the field is chosen per render
   * rather than baked in at mount -- the setting can flip while this view is open.
   * on_disk can be absent even when the setting is on (a row the engine could not stat),
   * hence the type check rather than a bare read.
   */
  function sizeField(row) {
    var s = settings();
    return (s && s.size_on_disk === true && typeof row.on_disk === 'number') ? 'on_disk' : 'size';
  }

  /*
   * row.admin_required together with a KNOWN admin === false is the only thing that
   * disables a row. An unknown admin state (admin() still null, because admin_state has
   * not answered yet) must not disable anything: telling the user a row is blocked when
   * we do not know is worse than letting the engine refuse it with its own sentence.
   */
  function isEnabled(row) {
    var st = ADC.app.admin();
    return !(row.admin_required && st && st.admin === false);
  }

  function displayName(id) {
    var e = state.byId[id];
    return (e && i18n.pick(e.row.name)) || String(id);
  }

  /*
   * Object.keys, filtered to rows that are still enabled. A gated id can therefore never
   * reach a selection the engine would refuse (docs/01-AUDIT BUG-04: v1 let one be ticked
   * and then failed at run time), even if the admin state changed after it was ticked.
   */
  function selectedIds() {
    var ids = Object.keys(state.sel);
    var out = [];
    for (var i = 0; i < ids.length; i += 1) {
      var e = state.byId[ids[i]];
      if (e && e.enabled) { out.push(ids[i]); }
    }
    return out;
  }
  /*
   * Sum the measured part of a set of rows, and only the measured part. A row nobody
   * scanned contributes nothing -- not a zero, which would read as "already empty" -- and
   * if any contributing row hit the walk budget then the whole figure is a lower bound,
   * so the flag is carried out for ui.size to render as the ">=" form.
   */
  function measure(entries, selectedOnly) {
    var out = { total: 0, seen: 0, truncated: false };
    for (var i = 0; i < entries.length; i += 1) {
      var e = entries[i];
      if (selectedOnly && !(e.enabled && state.sel[e.id])) { continue; }
      var row = state.scan[e.id];
      if (!row || row.measurable === false) { continue; }
      var n = row[sizeField(row)];
      if (typeof n !== 'number') { continue; }
      out.total += n;
      out.seen += 1;
      if (row.truncated) { out.truncated = true; }
    }
    return out;
  }

  /* A synthetic row for ui.size. ui.size takes the ROW rather than the number so the
     honesty flags cannot be dropped on the way, and a total has the same two honesty
     cases as a single figure: unknown, or a lower bound. */
  function totalRow(m) {
    return m.seen ? { size: m.total, truncated: m.truncated } : { size: null };
  }

  /* --- one target row ------------------------------------------------------------ */

  /*
   * ui.targetRow carries the parts a view would forget: the label/for wiring, the
   * disabled-with-a-stated-reason case, aria-describedby onto that reason. What it does
   * NOT expose is a handle to its note line -- `note` is rendered once at construction --
   * so the static rebuild cost goes in there and the scan's own reason line is a slot of
   * ours in the right-hand cell, filled and refilled as the walk reports.
   */
  function buildRow(row) {
    var entry = {
      id: row.id, row: row, enabled: isEnabled(row),
      node: null, sizeSlot: null, noteSlot: null
    };
    var sizeSlot = ui.el('span', { class: 'clean__size' });
    var noteSlot = ui.el('p', { class: 'clean__rownote', hidden: true });

    /* The risk tier, always as icon + word: ui.targetRow's `risk` option only sets a
       colour class, and docs/02-SPEC.md 7.3 forbids colour as the only channel. */
    var meta = [ui.riskBadge(row.risk, { compact: true })];
    if (row.admin_required) {
      meta.push(ui.tag({ i18n: 'tag.admin', icon: 'icon-lock', tone: 'warn' }));
    }
    if (row.reversible === false) {
      meta.push(ui.tag({ i18n: 'tag.irreversible', icon: 'icon-dangerous', tone: 'danger' }));
    }
    if (row.standalone) {
      meta.push(ui.tag({ i18n: 'tag.standalone', icon: 'icon-info' }));
    }
    var est = i18n.pick(row.est_note);
    if (est) { meta.push(ui.tag({ text: est })); }
    var reveal = ui.btn({
      icon: 'icon-folder', label: 'clean.reveal', variant: 'ghost', size: 'sm',
      class: 'clean__reveal',
      on: { click: function () { onReveal(row.id); } }
    });

    /*
     * The one row that opens a screen instead of ticking a box. `standalone` already says
     * it cannot travel with a batch; this says where it can be done instead, because a
     * user hunting for space finds shadow copies here and nowhere else -- the manager has
     * no nav item (docs/02-SPEC.md 5, and views/vss.js's own header).
     */
    var right = [ui.el('div', { class: 'clean__figure' }, [sizeSlot, noteSlot])];
    if (row.id === 'vss_manage') {
      right.push(ui.btn({
        i18n: 'clean.vss_open', icon: 'icon-shield', variant: 'ghost', size: 'sm',
        class: 'clean__open',
        on: { click: function () { ADC.app.go('vss'); } }
      }));
    }
    right.push(reveal);

    var node = ui.targetRow({
      id: row.id,
      name: i18n.pick(row.name),
      desc: i18n.pick(row.desc),
      risk: row.risk,
      /* A gated row is never shown ticked. A checked box the run will ignore is exactly
         the lie this component exists to prevent. */
      checked: entry.enabled && state.sel[row.id] === true,
      disabled: !entry.enabled,
      reason: entry.enabled ? null : i18n.t('reason.needs_admin'),
      /* rebuild_cost is what makes "rebuildable" mean something: the cache does come
         back, and this says what getting it back costs. */
      note: i18n.pick(row.rebuild_cost) || null,
      meta: meta,
      right: right,
      on: {
        change: function (evt) { onToggleRow(entry, evt.target.checked === true); }
      }
    });

    entry.node = node;
    entry.sizeSlot = sizeSlot;
    entry.noteSlot = noteSlot;
    paintRow(entry);
    return entry;
  }

  /*
   * Render whatever the scan currently knows about one row. Called for every row on every
   * poll (49 cells, four times a second, which is nothing) rather than diffing, because
   * partial_results hands over the whole list-so-far each time and a diff would be more
   * code for no gain.
   */
  function paintRow(entry) {
    var row = state.scan[entry.id];
    ui.clear(entry.sizeSlot);
    var note = '';
    if (row) {
      entry.sizeSlot.appendChild(ui.size(row, sizeField(row)));
      /* reason and error are free-text English from the engine ("path absent", "access
         denied", a walk that refused). They are rendered, never translated -- and only
         when they explain an ABSENCE, because a measured row's reason is not a sentence
         anybody needs to read. */
      note = row.error ? row.error
        : ((row.available === false || row.measurable === false) ? (row.reason || '') : '');
    }
    entry.noteSlot.textContent = note ? String(note) : '';
    entry.noteSlot.hidden = !note;
  }

  function onToggleRow(entry, checked) {
    if (checked) { state.sel[entry.id] = true; } else { delete state.sel[entry.id]; }
    refreshFooter();
    refreshGroupToggles();
  }
  function onReveal(id) {
    api.reveal(id).then(function (data) {
      ui.toast(i18n.t('clean.revealed'), {
        kind: 'info',
        detail: data && data.opened ? String(data.opened) : null
      });
    }, function (err) {
      /* Most of the catalogue is absent on any given machine, so "not on this machine" is
         a fact about the box and not an error to go red about. */
      if (err && err.code === 'not_present') {
        ui.toast(err.text(), { kind: 'warn' });
        return;
      }
      ui.showError(err);
    });
  }

  /* --- the catalogue, grouped ----------------------------------------------------- */

  /*
   * Grouped by category in the order the categories FIRST APPEAR in catalog.targets. The
   * engine's order is deliberate (dev before browser before system, cheap and safe before
   * expensive and scary), so nothing here re-sorts it.
   */
  function rebuildCatalogue() {
    ui.clear(dom.cats);
    state.rows = [];
    state.byId = {};
    state.groups = [];

    var cat = state.catalog;
    if (!cat || !cat.targets || !cat.targets.length) {
      dom.cats.appendChild(ui.emptyState({ icon: 'icon-info', i18n: 'clean.no_targets' }));
      return;
    }

    var order = [];
    var buckets = {};
    for (var i = 0; i < cat.targets.length; i += 1) {
      var row = cat.targets[i];
      var key = row.category || 'app';
      if (!buckets[key]) { buckets[key] = []; order.push(key); }
      buckets[key].push(row);
    }
    for (var g = 0; g < order.length; g += 1) {
      dom.cats.appendChild(buildGroup(order[g], buckets[order[g]]));
    }

    /* A rebuild can change what is gated (the admin state arrived, or the refresh button
       re-asked), so drop anything from the selection that is no longer selectable before
       the footer counts it. */
    pruneSelection();
    refreshSubtotals();
    refreshFooter();
    refreshGroupToggles();
    setLocked(working());
  }

  function pruneSelection() {
    var ids = Object.keys(state.sel);
    for (var i = 0; i < ids.length; i += 1) {
      var e = state.byId[ids[i]];
      if (!e || !e.enabled) { delete state.sel[ids[i]]; }
    }
  }
  function buildGroup(key, rows) {
    var group = {
      key: key, entries: [], subtotal: null, value: null, toggle: null, label: null
    };
    var value = ui.el('span', { class: 'clean__subtotal-value' });
    /* Hidden until the scan has produced at least one number for this category: a
       subtotal of nothing is not a zero. */
    var subtotal = ui.el('span', { class: 'clean__subtotal', hidden: true }, [
      ui.el('span', { class: 'clean__subtotal-label', i18n: 'clean.subtotal' }),
      value
    ]);
    var toggle = ui.btn({
      i18n: 'clean.cat.all', variant: 'ghost', size: 'sm', class: 'clean__cat-toggle',
      on: { click: function () { toggleGroup(group); } }
    });

    var card = ui.card({
      i18n: 'cat.' + key,
      icon: ui.CATEGORY_ICON[key] || 'icon-folder',
      actions: [subtotal, toggle],
      class: 'clean__cat'
    });
    for (var i = 0; i < rows.length; i += 1) {
      var entry = buildRow(rows[i]);
      state.rows.push(entry);
      state.byId[entry.id] = entry;
      group.entries.push(entry);
      card.body.appendChild(entry.node);
    }

    group.subtotal = subtotal;
    group.value = value;
    group.toggle = toggle;
    /* ui.btn puts the label in the span it created from the i18n key. Keeping the
       reference means the all/none flip is a relabel of one node, and because the
       data-i18n attribute is rewritten with it, i18n.apply() keeps it right through a
       language switch without this view doing anything. */
    group.label = toggle.querySelector('[data-i18n]');
    state.groups.push(group);
    return card;
  }

  function groupCounts(group) {
    var out = { n: 0, sel: 0 };
    for (var i = 0; i < group.entries.length; i += 1) {
      var e = group.entries[i];
      if (!e.enabled) { continue; }
      out.n += 1;
      if (state.sel[e.id]) { out.sel += 1; }
    }
    return out;
  }

  /* One control per category, and it is a genuine two-state toggle, so it carries
     aria-pressed as well as swapping its own label. */
  function toggleGroup(group) {
    if (working()) { return; }
    var counts = groupCounts(group);
    var turnOn = !(counts.n > 0 && counts.sel === counts.n);
    for (var i = 0; i < group.entries.length; i += 1) {
      var e = group.entries[i];
      if (!e.enabled) { continue; }
      if (turnOn) { state.sel[e.id] = true; } else { delete state.sel[e.id]; }
    }
    syncBoxes();
    refreshFooter();
    refreshGroupToggles();
  }
  function refreshGroupToggles() {
    for (var g = 0; g < state.groups.length; g += 1) {
      var group = state.groups[g];
      var counts = groupCounts(group);
      var all = counts.n > 0 && counts.sel === counts.n;
      group.toggle.disabled = counts.n === 0 || working();
      group.toggle.setAttribute('aria-pressed', all ? 'true' : 'false');
      var key = all ? 'clean.cat.none' : 'clean.cat.all';
      if (group.label) {
        group.label.setAttribute('data-i18n', key);
        group.label.textContent = i18n.t(key);
      }
    }
  }

  function refreshSubtotals() {
    for (var g = 0; g < state.groups.length; g += 1) {
      var group = state.groups[g];
      var m = measure(group.entries, false);
      ui.clear(group.value);
      group.value.appendChild(ui.size(totalRow(m)));
      group.subtotal.hidden = !m.seen;
    }
  }

  function syncBoxes() {
    for (var i = 0; i < state.rows.length; i += 1) {
      var e = state.rows[i];
      e.node.box.checked = e.enabled && state.sel[e.id] === true;
    }
  }

  function repaintSizes() {
    for (var i = 0; i < state.rows.length; i += 1) { paintRow(state.rows[i]); }
    refreshSubtotals();
    refreshFooter();
  }

  /* The selection is frozen while a job runs. Not for safety -- the engine froze its own
     copy of the id list when the job started -- but because a box the user unticks
     mid-clean would still be in the plan, and that reads as the UI lying. */
  function setLocked(locked) {
    for (var i = 0; i < state.rows.length; i += 1) {
      var e = state.rows[i];
      e.node.box.disabled = !e.enabled || locked;
    }
    dom.cats.classList.toggle('clean__cats--locked', locked);
  }

  /* --- the preset toolbar --------------------------------------------------------- */

  /*
   * One button per key of catalog.presets, plus a clear. These are actions rather than
   * toggles -- pressing one replaces the selection -- so the one that matches
   * settings().preset is marked with aria-current, not aria-pressed: it is the user's
   * default, and the selection may since have been curated away from it.
   */
  function buildPresets() {
    ui.clear(dom.presets);
    dom.presetBtns = {};
    var names = Object.keys((state.catalog && state.catalog.presets) || {});
    for (var i = 0; i < names.length; i += 1) {
      dom.presets.appendChild(presetBtn(names[i]));
    }
    dom.presets.appendChild(presetBtn(null));
    refreshPresets();
  }
  function presetBtn(name) {
    var b = ui.btn({
      i18n: name ? 'clean.preset.' + name : 'clean.preset.none',
      icon: name ? 'icon-shield' : 'icon-close',
      variant: 'ghost',
      class: 'clean__preset',
      on: { click: function () { applyPreset(name); } }
    });
    dom.presetBtns[name === null ? '' : name] = b;
    return b;
  }

  function applyPreset(name) {
    if (working()) { return; }
    var presets = (state.catalog && state.catalog.presets) || {};
    setSelection(name === null ? [] : (presets[name] || []));
  }

  function setSelection(ids) {
    state.sel = {};
    for (var i = 0; i < ids.length; i += 1) {
      var e = state.byId[ids[i]];
      /* A gated id never enters the set. The engine would refuse it, and selectedIds
         filters it anyway -- keeping it out here means the boxes and the count agree. */
      if (e && e.enabled) { state.sel[e.id] = true; }
    }
    syncBoxes();
    refreshFooter();
    refreshGroupToggles();
  }

  function refreshPresets() {
    var s = settings();
    var current = s && s.preset ? s.preset : '';
    var keys = Object.keys(dom.presetBtns);
    for (var i = 0; i < keys.length; i += 1) {
      var b = dom.presetBtns[keys[i]];
      var on = keys[i] !== '' && keys[i] === current;
      b.classList.toggle('btn--primary', on);
      b.classList.toggle('btn--ghost', !on);
      if (on) { b.setAttribute('aria-current', 'true'); } else { b.removeAttribute('aria-current'); }
      b.disabled = working();
    }
  }

  /* --- the footer ------------------------------------------------------------------ */

  function buildFooter() {
    dom.count = ui.el('span', { class: 'clean__count' });
    dom.totalSlot = ui.el('span', { class: 'clean__total-value' });
    var status = ui.el('div', { class: 'clean__status' }, [
      dom.count,
      ui.el('span', { class: 'clean__total' }, [
        ui.el('span', { class: 'clean__total-label', i18n: 'clean.total' }),
        dom.totalSlot
      ])
    ]);

    dom.progress = ui.progress();
    /* The live region goes on the wrapper, not on the bar: the bar already carries
       aria-valuenow, and what a screen reader needs spoken is the phase sentence under it.
       polite, because a scan emits four of these a second and assertive would talk over
       itself continuously. */
    dom.progressWrap = ui.el('div', {
      class: 'clean__progress', hidden: true, role: 'status',
      attrs: { 'aria-live': 'polite' }
    }, [dom.progress]);
    dom.scanBtn = ui.btn({
      i18n: 'clean.scan', icon: 'icon-search', variant: 'ghost',
      class: 'clean__scan', on: { click: onScan }
    });
    dom.previewBtn = ui.btn({
      i18n: 'clean.preview', icon: 'icon-shield', variant: 'primary',
      class: 'clean__preview', on: { click: onPreview }
    });
    dom.stopBtn = ui.btn({
      i18n: 'action.stop', icon: 'icon-stop', variant: 'danger',
      class: 'clean__stop', on: { click: onStop }
    });
    dom.stopBtn.hidden = true;

    return ui.el('div', { class: 'clean__footer' }, [
      status,
      dom.progressWrap,
      ui.el('div', { class: 'clean__actions' }, [dom.scanBtn, dom.previewBtn, dom.stopBtn])
    ]);
  }

  function refreshFooter() {
    var ids = selectedIds();
    dom.count.textContent = i18n.tn('clean.selected', ids.length);
    ui.clear(dom.totalSlot);
    dom.totalSlot.appendChild(ui.size(totalRow(measure(state.rows, true))));
  }

  /* --- phases ---------------------------------------------------------------------- */

  function setPhase(next) {
    state.phase = next;
    var running = next === SCANNING || next === CLEANING;
    var busy = working();
    dom.scanBtn.disabled = busy;
    dom.previewBtn.disabled = busy;
    dom.stopBtn.hidden = !running;
    if (running) { dom.stopBtn.disabled = false; }
    dom.progressWrap.hidden = !busy;
    dom.log.setBusy(running);
    setLocked(busy);
    refreshPresets();
    refreshGroupToggles();
    if (!busy) { dom.progress.set(0, ''); }
  }

  /*
   * The one-job guard. The engine is the authority -- it answers `busy` and that refusal
   * is final -- and this only stops the UI provoking it. Claim BEFORE the bridge call,
   * release when the job settles, in both the success and the failure path.
   *
   * The closure is wrapped so it can be called twice harmlessly: the settle path and the
   * failure path both end up here, and releasing a claim twice would unlock a job that is
   * still running.
   */
  function claim(kind) {
    var rel = ADC.app.claimJob(kind);
    if (!rel) {
      var busyKind = ADC.app.busyWith();
      ui.toast(i18n.t('clean.busy', { kind: i18n.t('kind.' + (busyKind || 'unknown')) }),
        { kind: 'warn' });
      return null;
    }
    var spent = false;
    return function () {
      if (spent) { return; }
      spent = true;
      rel();
    };
  }
  /* --- watching a job -------------------------------------------------------------- */

  function attach(jobId, kind) {
    var w = api.watch(jobId, {
      onEvents: function (events) { dom.log.push(events); },
      onSnapshot: function (snap) { onSnapshot(snap, kind); }
    });
    state.watcher = w;
    /* The promise resolves for EVERY outcome including failed and cancelled -- those are
       things this view renders, not exceptions. It rejects only when polling itself became
       impossible. */
    w.promise.then(function (snap) {
      onSettled(snap, kind);
    }, function (err) {
      /* no_such_job on a re-attach is ordinary: the job finished while the user was on
         another view and the engine has already forgotten it. Nothing to shout about --
         drop the claim and go quiet. */
      if (err && err.code === 'no_such_job') {
        finishJob();
        setPhase(restPhase());
        return;
      }
      finishJob();
      setPhase(restPhase());
      ui.showError(err);
    });
  }

  function finishJob() {
    if (state.release) { state.release(); state.release = null; }
    state.job = null;
    state.watcher = null;
    state.live = null;
  }

  function onSnapshot(snap, kind) {
    state.live = snap;
    /* pct is 0..1 from the engine and 0..100 here (jobs.py; ui.progress().set). */
    dom.progress.set((typeof snap.pct === 'number' ? snap.pct : 0) * 100,
      i18n.pickField(snap, 'detail'));
    if (kind === 'scan') { ingestScan(snap); }
    /* Once the engine has the cancel request there is nothing more to ask for. */
    if (snap.cancel_requested) { dom.stopBtn.disabled = true; }
  }

  /*
   * Fill the size cells from the running walk. partial_results.rows is the whole
   * list-so-far on every poll, so the table GROWS during the scan -- that is the
   * requirement (docs/02-SPEC.md 3.1), not a nicety.
   */
  function ingestScan(snap) {
    var pr = snap.partial_results;
    if (!pr || pr.kind !== 'scan' || !pr.rows || !pr.rows.length) { return; }
    for (var i = 0; i < pr.rows.length; i += 1) {
      var row = pr.rows[i];
      state.scan[row.target_id] = row;
      var e = state.byId[row.target_id];
      if (e) { paintRow(e); }
    }
    refreshSubtotals();
    refreshFooter();
  }

  function onSettled(snap, kind) {
    finishJob();
    if (kind === 'scan') { finishScan(snap); } else { finishClean(snap); }
  }
  function onStop() {
    if (!state.watcher) { return; }
    dom.stopBtn.disabled = true;
    ui.toast(i18n.t('clean.stopping'), { kind: 'info' });
    /* watch().cancel() sends job_cancel and, as bridge.js is actually written
       (bridge.js:203-207), leaves this page polling -- which is what we want here: the
       cancelled snapshot still arrives, so the ordinary settle path releases the claim and
       renders the partial result exactly once. Its own comment claims it stops polling
       too; the code does not, and the code is the contract. */
    state.watcher.cancel();
  }

  /* --- the scan -------------------------------------------------------------------- */

  function onScan() {
    if (working()) { return; }
    var release = claim('scan');
    if (!release) { return; }
    var ids = selectedIds();
    if (!ids.length) {
      /* The engine would answer empty_selection; refusing here is faster and the sentence
         can be about the checkboxes instead of about the API. */
      release();
      ui.toast(i18n.t('clean.nothing_selected'), { kind: 'warn' });
      return;
    }
    state.release = release;
    setPhase(SCANNING);
    dom.progress.set(0, i18n.t('clean.starting'));
    /* volumeIds is null on purpose: null means "whatever the user saved", so the engine
       reads the tick list off the config file itself (bridge.scan_start) rather than
       trusting this page's cached settings copy. */
    api.scanStart(null, ids).then(function (data) {
      state.job = { id: data.job_id, kind: 'scan' };
      attach(data.job_id, 'scan');
    }, function (err) {
      finishJob();
      setPhase(restPhase());
      ui.showError(err);
    });
  }

  function finishScan(snap) {
    ingestScan(snap);
    setPhase(restPhase());
    if (snap.state === 'cancelled') {
      ui.toast(i18n.t('clean.cancelled'), { kind: 'warn' });
      return;
    }
    if (snap.state === 'failed') {
      ui.toast(i18n.t('clean.scan_failed'), {
        kind: 'error', detail: snap.error ? String(snap.error) : null
      });
      return;
    }
    var totals = (snap.partial_results && snap.partial_results.totals) || null;
    /* total_size counts only the rows actually walked, and truncated is the whole-scan
       flag, so this sentence is a lower bound whenever any single row was. */
    ui.toast(i18n.t('clean.scan_done', {
      size: i18n.fmtBytes(totals ? totals.total_size : null,
        { truncated: !!(totals && totals.truncated) })
    }), { kind: 'success' });
  }
  /* --- preview -> confirm -> execute ------------------------------------------------ */

  function onPreview() {
    if (working()) { return; }
    /* A fresh press of the button is a fresh chance at the one automatic re-preview. */
    state.planRetried = false;
    preview();
  }

  function preview() {
    var ids = selectedIds();
    if (!ids.length) {
      ui.toast(i18n.t('clean.nothing_selected'), { kind: 'warn' });
      return;
    }
    setPhase(PLANNING);
    dom.progress.set(0, i18n.t('clean.previewing'));
    /* allowDangerous false, always, for the first pass: it SKIPS every dangerous row and
       only names them in dangerous_ids. Nothing dangerous can be included until the
       handshake below re-prices the plan with them in. */
    api.cleanPlan(ids, false).then(onPlan, planFailed);
  }

  function planFailed(err) {
    setPhase(restPhase());
    ui.showError(err);
  }

  function onPlan(plan) {
    var dangerous = plan.dangerous_ids || [];
    if (!dangerous.length) { askExecute(plan); return; }
    var s = settings();
    /* confirm_dangerous governs the EXTRA handshake, not whether dangerous rows are in the
       plan -- the first preview skipped them either way, so both branches have to re-price
       with allowDangerous true. The final confirmation still states the figure and the
       count, so turning this off loses the second question and nothing else. An unknown
       settings object counts as "ask". */
    if (s && s.confirm_dangerous === false) { reprice(); return; }
    ui.confirm({
      danger: true,
      i18n: 'clean.danger.title',
      body: 'clean.danger.body',
      lines: dangerous.map(displayName),
      confirmKey: 'clean.danger.confirm'
    }).then(function (ok) {
      if (!ok) {
        /* Leave the checkboxes exactly as they were. The user said no to this run, not to
           their selection. */
        setPhase(restPhase());
        return;
      }
      reprice();
    });
  }

  function reprice() {
    api.cleanPlan(selectedIds(), true).then(askExecute, planFailed);
  }
  /*
   * docs/02-SPEC.md 4.6: the row count and the byte total, in words, before anything is
   * deleted. Every other line is here because it changes what the user is agreeing to --
   * rows that will be skipped, rows that need Administrator and will not get it, rows that
   * cannot be undone, the age floor in force, and how many exclusions are filtering it.
   *
   * count and est_total describe ONLY the rows that will_run: a skipped row's estimate is
   * not a promise, so it is never added into the figure.
   */
  function confirmLines(plan) {
    var lines = [
      i18n.t('clean.confirm.rows', { n: i18n.fmtInt(plan.count) }),
      i18n.t('clean.confirm.total', {
        size: i18n.fmtBytes(plan.est_total, { truncated: plan.truncated === true })
      }),
      plan.skipped ? i18n.t('clean.confirm.skipped', { n: i18n.fmtInt(plan.skipped) }) : null,
      (plan.needs_admin && !plan.is_admin) ? i18n.t('clean.confirm.needs_admin') : null,
      plan.has_irreversible ? i18n.t('clean.confirm.irreversible') : null,
      plan.min_age_hours > 0
        ? i18n.t('clean.confirm.min_age', { hours: i18n.fmtInt(plan.min_age_hours) })
        : i18n.t('clean.confirm.min_age_off'),
      plan.exclusion_count
        ? i18n.t('clean.confirm.exclusions', { n: i18n.fmtInt(plan.exclusion_count) })
        : null
    ];
    return lines.filter(function (line) { return !!line; });
  }

  function askExecute(plan) {
    if (!plan.count) {
      /* Everything was skipped: too recent for min_age_hours, excluded, or not on this
         machine. There is nothing to confirm, and a dialog offering to delete nothing is
         noise rather than caution. */
      setPhase(restPhase());
      ui.toast(i18n.t('clean.nothing_to_clean'), { kind: 'warn' });
      return;
    }
    ui.confirm({
      i18n: 'clean.confirm.title',
      danger: plan.has_irreversible === true,
      confirmKey: 'clean.confirm.ok',
      lines: confirmLines(plan)
    }).then(function (ok) {
      if (!ok) { setPhase(restPhase()); return; }
      execute(plan);
    });
  }

  function execute(plan) {
    var release = claim('clean');
    if (!release) {
      setPhase(restPhase());
      return;
    }
    state.release = release;
    setPhase(CLEANING);
    dom.progress.set(0, i18n.t('clean.starting'));
    api.cleanExecute(plan.token).then(function (data) {
      state.job = { id: data.job_id, kind: 'clean' };
      attach(data.job_id, 'clean');
    }, function (err) {
      if (err && STALE_PLAN[err.code] && !state.planRetried) {
        state.planRetried = true;
        if (state.release) { state.release(); state.release = null; }
        reprice();
        return;
      }
      finishJob();
      setPhase(restPhase());
      ui.showError(err);
    });
  }

  function finishClean(snap) {
    state.snap = snap;
    setPhase(restPhase());
    renderSummary();
    if (snap.id) { fetchReport(snap.id, true); }
    if (snap.state === 'cancelled') {
      ui.toast(i18n.t('clean.cancelled'), { kind: 'warn' });
      return;
    }
    if (snap.state === 'failed') {
      ui.toast(snap.error || i18n.t('state.failed'), {
        kind: 'error', detail: snap.error ? String(snap.error) : null
      });
      return;
    }
    ui.toast(i18n.t('state.done'), { kind: 'success' });
  }

  function fetchReport(jobId, retry) {
    api.reportDetail(jobId).then(function (data) {
      if (state.snap && state.snap.id === jobId) {
        state.report = (data && data.report) || null;
        renderSummary();
      }
    }, function () {
      if (retry) {
        window.setTimeout(function () {
          fetchReport(jobId, false);
        }, REPORT_RETRY_MS);
      }
    });
  }

  function renderSummary() {
    ui.clear(dom.summary);
    var snap = state.snap;
    if (!snap) {
      dom.summary.hidden = true;
      return;
    }
    dom.summary.hidden = false;

    var dismissBtn = ui.btn({
      icon: 'icon-close',
      label: 'action.dismiss',
      variant: 'ghost',
      size: 'sm',
      on: {
        click: function () {
          state.snap = null;
          state.report = null;
          setPhase(restPhase());
          renderSummary();
        }
      }
    });

    var histBtn = ui.btn({
      icon: 'icon-history',
      i18n: 'nav.history',
      variant: 'ghost',
      size: 'sm',
      on: {
        click: function () { ADC.app.go('history'); }
      }
    });

    var card = ui.card({
      icon: 'icon-clean',
      i18n: 'history.reclaimed',
      actions: [histBtn, dismissBtn]
    });

    var stats = [
      ui.stat({
        i18n: 'history.reclaimed',
        value: i18n.fmtBytes(snap.reclaimed_total)
      })
    ];

    if (state.report && typeof state.report.free_delta_total === 'number') {
      stats.push(ui.stat({
        i18n: 'history.free_delta',
        value: i18n.fmtBytes(state.report.free_delta_total, { signed: true }),
        hint: i18n.t('history.free_delta.hint')
      }));
    }

    var touched = snap.per_target ? Object.keys(snap.per_target).length : 0;
    stats.push(ui.stat({
      i18n: 'history.touched',
      value: i18n.fmtInt(touched)
    }));

    ui.append(card.body, stats);
    dom.summary.appendChild(card);
  }

  function loadCatalogue() {
    if (state.catalogPending) { return; }
    state.catalogPending = true;
    var p = (ADC.app && typeof ADC.app.catalog === 'function') ? ADC.app.catalog() : api.catalog();
    p.then(function (cat) {
      state.catalogPending = false;
      state.catalog = cat;
      buildPresets();
      rebuildCatalogue();
      seedPreset();
    }, function (err) {
      state.catalogPending = false;
      ui.showError(err);
    });
  }

  function seedPreset() {
    if (state.seeded) { return; }
    var s = settings();
    var name = (s && s.preset) ? s.preset : 'safe';
    var presets = (state.catalog && state.catalog.presets) || {};
    if (presets[name]) {
      state.seeded = true;
      applyPreset(name);
    }
  }

  function mount(host) {
    dom.root = host;
    dom.summary = ui.el('div', { hidden: true });
    dom.presets = ui.el('div', { role: 'toolbar' });
    dom.cats = ui.el('div');
    var footer = buildFooter();
    dom.log = ui.consolePane();

    host.appendChild(dom.summary);
    host.appendChild(dom.presets);
    host.appendChild(dom.cats);
    host.appendChild(footer);
    host.appendChild(dom.log);

    setPhase(IDLE);
    loadCatalogue();
  }

  function enter(host, actions) {
    if (state.job && !state.watcher) {
      attach(state.job.id, state.job.kind);
    }
    if (!state.catalog) {
      loadCatalogue();
    } else {
      for (var i = 0; i < state.rows.length; i += 1) {
        var e = state.rows[i];
        e.enabled = isEnabled(e.row);
        if (e.node && e.node.box) {
          e.node.box.disabled = !e.enabled || working();
          e.node.classList.toggle('trow--disabled', !e.enabled);
        }
      }
      pruneSelection();
      syncBoxes();
      refreshSubtotals();
      refreshFooter();
      refreshGroupToggles();
    }
    setPhase(state.phase);
  }

  function leave() {
    if (state.watcher) {
      state.watcher.stopPolling();
      state.watcher = null;
    }
  }

  function relang() {
    if (state.catalog) {
      buildPresets();
      rebuildCatalogue();
    }
    if (state.snap) {
      renderSummary();
    }
    refreshFooter();
  }

  views.clean = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
