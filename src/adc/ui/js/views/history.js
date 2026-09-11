/*
 * views/history.js -- ADC.views.history. The receipts.
 *
 * Every other view in this app is about what might happen: a catalogue with no sizes in
 * it, a scan that estimates, a plan that expires in ten minutes. This one is the only
 * place that says what did happen, so its whole job is to be believable. It shows
 * MEASURED numbers only -- reclaimed_total is max(0, before - after), computed by the
 * engine after the fact and never estimated -- and it shows them beside the free space
 * the disks report right now, because the interesting end of a trend is the current one.
 * That is why the history() payload ships a live 'volumes' list at all.
 *
 * Two panes: the runs on the left, one run in full on the right.
 *
 * What this view deliberately does NOT do:
 *
 *   - poll. Nothing here changes on its own; the free-space strip is re-read on enter()
 *     and on the refresh button, and that is the whole of its liveness. leave() has
 *     nothing to stop and says so, because an absent leave() reads as an oversight.
 *   - start a job. history() and report_detail() are plain reads, so there is no
 *     ADC.app.claimJob() here -- the one-job guard belongs to the views that start one.
 *   - add a second "open the log file" button. ui.consolePane() already carries it, and
 *     open_log takes no argument, so a second one could not even point somewhere else.
 *   - rebuild the run list when the selection changes. That would destroy the <button>
 *     the user just pressed and drop keyboard focus onto <body>, so selecting only flips
 *     a class and aria-current on nodes that stay where they are.
 *   - re-fetch anything on a language switch. Both panes are redrawn from data already in
 *     hand: the language changed, the engine's numbers did not.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;
  /* ADC.app is deliberately NOT captured here. app.js is the last <script> in
     index.html, so at the moment this file executes the hub does not exist yet; every use
     below reads ADC.app at call time, which is always inside mount() or enter() and
     therefore always after app.js has run. */

  /* The list length. Fifty receipts is more than a user scrolls, and small enough that
     the engine reads fifty JSON files without the page noticing. */
  var LIMIT = 50;

  /* The glyph i18n prints for a number it cannot format (i18n.js:126). Reused for a cell
     with nothing in it, so "no note" and "no number" look the same rather than inventing
     a second convention. It is the one non-ASCII character in this file, defined once. */
  var DASH = '—';

  /*
   * Run states, straight off the engine's job state machine. Each carries a tone AND an
   * icon: docs/02-SPEC.md 7.3 forbids conveying a state by colour alone, so "failed" has
   * to survive greyscale and a screen reader -- which is why every tag below is built
   * with an icon and a word, never a colour on its own.
   *
   * 'pending' and 'running' cannot honestly appear in a finished report, but the engine
   * writes the field verbatim and a receipt from a run that died mid-flight could carry
   * either, so they get an entry rather than falling through to "unknown".
   */
  var STATE = {
    done: { tone: 'success', icon: 'icon-safe' },
    failed: { tone: 'danger', icon: 'icon-dangerous' },
    cancelled: { tone: 'warn', icon: 'icon-stop' },
    running: { tone: 'info', icon: 'icon-refresh' },
    pending: { tone: 'info', icon: 'icon-info' }
  };

  /* Two kinds of run. The kind is shown next to the state because a scan reclaims nothing
     by definition -- "0 B" on a scan receipt is the correct answer, not a failure, and
     without the kind beside it that figure reads as one. */
  var KIND = { scan: 'icon-search', clean: 'icon-clean' };

  /* --- module state ----------------------------------------------------------- */

  var dom = null;        /* the nodes mount() built, or null before the first visit */
  var reports = [];      /* history().reports, in the engine's order: newest first */
  var items = [];        /* [{jobId, node}] for the list buttons, for markSelection() */
  var vols = [];         /* the live volume list behind the free-space strip */
  var catRows = [];      /* catalogue rows, kept so names can be re-picked on relang() */
  var names = {};        /* target_id -> display name in the current language */
  var selected = null;   /* the selected job_id, or null */
  var report = null;     /* the loaded report for 'selected', cached for relang() */
  var detailErr = null;  /* the refusal for 'selected', cached for the same reason */
  var listErr = null;
  var volsErr = null;
  var detailSeq = 0;     /* see loadDetail(): the last click wins, not the last reply */

  /*
   * Engine payloads are JSON objects used as maps -- per_target is keyed by target id,
   * and a 'state' string indexes a table above. A key like "constructor" reaches
   * Object.prototype and comes back as a function, which is how a lookup like that turns
   * into a TypeError three lines later, so every map read goes through here.
   */
  function own(obj, key) {
    return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
  }

  /* An ApiError already carries a sentence the engine localised; anything else is a bug
     in this page and gets the generic line. */
  function errText(err) {
    return err && typeof err.text === 'function' ? err.text() : i18n.t('error.unexpected');
  }
  /* --- small builders --------------------------------------------------------- */

  function stateTag(state) {
    var known = own(STATE, state);
    var meta = known ? STATE[state] : { tone: 'info', icon: 'icon-info' };
    return ui.tag({
      tone: meta.tone,
      icon: meta.icon,
      i18n: known ? 'state.' + state : 'state.unknown'
    });
  }

  function kindTag(kind) {
    var known = own(KIND, kind);
    return ui.tag({
      icon: known ? KIND[kind] : 'icon-info',
      i18n: known ? 'kind.' + kind : 'kind.unknown'
    });
  }

  /* A label and a figure, so a bare "2 ph 14 gy" is never left to be guessed at. The
     value is already-formatted free text, which is exactly why relang() rebuilds:
     i18n.apply() can retranslate the label and cannot touch the number. */
  function pair(labelKey, value) {
    return ui.el('div', { class: 'hist__pair' }, [
      ui.el('span', { class: 'hist__pair-label', i18n: labelKey }),
      ui.el('span', { class: 'hist__pair-value num', text: value })
    ]);
  }

  /* scope="col" is not decoration: without it a screen reader reads the volume table as
     four unrelated numbers instead of a row about C:/. */
  function th(key) {
    return ui.el('th', { class: 'hist__th', i18n: key, attrs: { scope: 'col' } });
  }

  function numCell(text) {
    return ui.el('td', { class: 'hist__td num', text: text });
  }

  /* The catalogue is the only place a target's human name lives, and it may not have
     landed yet -- or a receipt may name a target this build no longer has. The raw id is
     the fallback, because an unrecognised id is still information. */
  function displayName(id) {
    return own(names, id) ? names[id] : String(id);
  }
  /* --- the free-space strip ---------------------------------------------------- */

  /*
   * One line per volume, above the list. The history() payload carries its own 'volumes'
   * array, but this reads ADC.app.volumes() instead so the figure here is the same
   * snapshot every other view is showing: two live reads a second apart would disagree by
   * a few megabytes and look like a bug in one of them.
   */
  function renderVols() {
    var box = ui.clear(dom.vols);
    if (volsErr) {
      box.appendChild(ui.el('p', { class: 'hist__vols-note', i18n: 'history.vols_failed' }));
      return;
    }
    if (!vols.length) {
      box.appendChild(ui.el('p', { class: 'hist__vols-note', i18n: 'history.vols_none' }));
      return;
    }
    for (var i = 0; i < vols.length; i += 1) {
      var v = vols[i];
      box.appendChild(ui.el('div', { class: 'hist__vol' }, [
        /* display_name is free text off the disk: a label a user typed, in any script. */
        ui.el('span', { class: 'hist__vol-name', text: v.display_name || v.root || v.letter }),
        ui.el('span', {
          class: 'hist__vol-free num',
          /* free_pct arrives already scaled 0-100 and i18n.fmtPct does not scale either
             (i18n.js:157-162), so it is passed straight through. Scaling it here would
             print "0.9%" for a disk that is nine tenths empty. */
          text: i18n.t('history.vol_free', {
            size: i18n.fmtBytes(v.free),
            pct: i18n.fmtPct(v.free_pct)
          })
        })
      ]));
    }
  }

  /* --- catalogue names -------------------------------------------------------- */

  /* Names are language-dependent (i18n.pick), so the map is rebuilt rather than fetched
     when the language changes -- catRows is the cached input that makes that free. */
  function rebuildNames() {
    var map = {};
    for (var i = 0; i < catRows.length; i += 1) {
      var row = catRows[i];
      if (row && row.id) { map[row.id] = i18n.pick(row.name); }
    }
    names = map;
  }
  /* Fetched through the hub, so this is one shared promise for all seven views and costs
     nothing after the first call. It can land after a report is already on screen -- that
     report was drawn with raw ids, so redraw it once the names exist. */
  function loadCatalog() {
    ADC.app.catalog().then(function (cat) {
      catRows = (cat && cat.targets) || [];
      rebuildNames();
      if (report) { renderDetail(); }
    }, function (err) {
      /* A missing catalogue costs display names and nothing else. Every number on this
         page is still correct, so it is not worth a toast; the console keeps it for a
         developer. */
      if (window.console) { console.error('history: catalog failed', err); }
    });
  }

  /* --- loading ---------------------------------------------------------------- */

  /*
   * Both reads go out together rather than one after the other: the list is a directory
   * of JSON files and the volume list is a disk query, and chaining them would add the
   * slower one's latency to the faster one for nothing.
   */
  function load() {
    api.history(LIMIT).then(function (data) {
      listErr = null;
      reports = (data && data.reports) || [];
      /* A receipt can disappear between visits -- the engine trims its report directory.
         Holding a detail pane open for a run that is no longer in the list would be
         showing something the user can no longer navigate back to. */
      if (selected !== null && !hasReport(selected)) {
        selected = null;
        report = null;
        detailErr = null;
        renderDetail();
      }
      renderList();
    }, function (err) {
      listErr = err;
      reports = [];
      renderList();
      ui.showError(err);
    });

    ADC.app.volumes(true).then(function (data) {
      volsErr = null;
      vols = (data && data.volumes) || [];
      renderVols();
    }, function (err) {
      volsErr = err;
      vols = [];
      renderVols();
      ui.showError(err);
    });
  }
  function hasReport(jobId) {
    for (var i = 0; i < reports.length; i += 1) {
      if (reports[i] && reports[i].job_id === jobId) { return true; }
    }
    return false;
  }

  /* --- the run list ----------------------------------------------------------- */

  /*
   * A <button>, not a <div> with a click handler. The list is the primary control of this
   * view, so every entry has to be reachable by Tab and activated by Enter or Space --
   * which a div is not, and cannot be made to be by adding a role alone.
   */
  function listItem(r) {
    var node = ui.el('button', {
      class: 'hist__item',
      type: 'button',
      on: { click: function () { select(r.job_id); } }
    }, [
      ui.el('span', { class: 'hist__item-when num', text: i18n.fmtDate(r.finished_at) }),
      ui.el('div', { class: 'hist__item-tags' }, [stateTag(r.state), kindTag(r.kind)]),
      ui.el('div', { class: 'hist__item-figs' }, [
        ui.el('span', { class: 'hist__item-size num', text: i18n.fmtBytes(r.reclaimed_total) }),
        ui.el('span', { class: 'hist__item-dur num', text: i18n.fmtDuration(r.duration_s) }),
        ui.el('span', {
          class: 'hist__item-targets',
          text: i18n.tn('history.targets', (r.targets || []).length)
        })
      ])
    ]);
    items.push({ jobId: r.job_id, node: node });
    return node;
  }

  function renderList() {
    var box = ui.clear(dom.list);
    items = [];
    if (listErr) {
      box.appendChild(ui.emptyState({
        icon: 'icon-caution', i18n: 'history.load_failed', text: errText(listErr)
      }));
      return;
    }
    if (!reports.length) {
      box.appendChild(ui.emptyState({
        icon: 'icon-history', i18n: 'history.empty', body: 'history.empty.body'
      }));
      return;
    }
    for (var i = 0; i < reports.length; i += 1) { box.appendChild(listItem(reports[i])); }
    markSelection();
  }
  /* Selection is a class flip plus aria-current, never a rebuild -- see the header for
     what a rebuild would do to keyboard focus. */
  function markSelection() {
    for (var i = 0; i < items.length; i += 1) {
      var on = items[i].jobId === selected;
      items[i].node.classList.toggle('is-active', on);
      if (on) { items[i].node.setAttribute('aria-current', 'true'); }
      else { items[i].node.removeAttribute('aria-current'); }
    }
  }

  function select(jobId) {
    /* Clicking the selected row again is a no-op, unless the last read failed -- then it
       is the obvious way to retry and pretending otherwise leaves the user stuck. */
    if (jobId === selected && !detailErr) { return; }
    selected = jobId;
    report = null;
    detailErr = null;
    markSelection();
    renderDetail();
    loadDetail(jobId);
  }

  /*
   * report_detail reads one JSON file, so it is fast, but a user clicking down a list of
   * fifty can still have two reads in flight. The sequence number makes the LAST CLICK
   * win rather than the last reply: without it a slow first read lands after a quick
   * second one, and the pane ends up describing a run the list is not pointing at.
   */
  function loadDetail(jobId) {
    detailSeq += 1;
    var seq = detailSeq;
    api.reportDetail(jobId).then(function (data) {
      if (seq !== detailSeq) { return; }
      report = (data && data.report) || null;
      detailErr = null;
      renderDetail();
    }, function (err) {
      if (seq !== detailSeq) { return; }
      report = null;
      detailErr = err;
      /* no_such_report is an ordinary outcome, not a malfunction: the receipt file was
         deleted or the profile directory moved. The pane states it, so a toast on top
         would be saying it twice. Anything else is unexpected and does get one. */
      if (!err || err.code !== 'no_such_report') { ui.showError(err); }
      renderDetail();
    });
  }
  /* --- the detail pane -------------------------------------------------------- */

  /*
   * {seconds: true} on both ends of the range, deliberately: most cleans finish inside a
   * minute, and a header reading "17:42 to 17:42" looks like a rendering fault rather than
   * a fast run.
   */
  function detailHead(body) {
    var tags = ui.el('div', { class: 'hist__tags' }, [
      stateTag(body.state), kindTag(body.kind)
    ]);
    if (body.cancelled) {
      /* Distinct from the state tag on purpose: "cancelled" is the outcome, this says who
         ended it. A run can finish its work after the request and still carry the flag. */
      tags.appendChild(ui.tag({
        tone: 'warn', icon: 'icon-stop', i18n: 'history.stopped_by_user'
      }));
    }
    return ui.el('header', { class: 'hist__head' }, [
      ui.el('h2', {
        class: 'hist__title num',
        text: i18n.t('history.range', {
          from: i18n.fmtDate(body.started_at, { seconds: true }),
          to: i18n.fmtDate(body.finished_at, { seconds: true })
        })
      }),
      pair('history.duration', i18n.fmtDuration(body.duration_s)),
      tags
    ]);
  }

  /*
   * Three figures. reclaimed_total and free_delta_total go through fmtBytes rather than
   * ui.size() on purpose: ui.size() takes a ROW so it can carry 'measurable: false' and
   * the ">=" of a walk that hit its budget, and a finished report has neither flag -- its
   * total is a measured before-minus-after, and dressing it in the estimate machinery
   * would suggest it is an estimate.
   */
  function detailStats(body) {
    var touched = body.per_target ? Object.keys(body.per_target).length : 0;
    return ui.el('div', { class: 'hist__stats' }, [
      ui.stat({ i18n: 'history.reclaimed', value: i18n.fmtBytes(body.reclaimed_total) }),
      ui.stat({
        i18n: 'history.free_delta',
        value: i18n.fmtBytes(body.free_delta_total, { signed: true }),
        /* A translated sentence handed over as free text, which is why relang() rebuilds
           this pane instead of leaving it to i18n.apply(). */
        hint: i18n.t('history.free_delta.hint')
      }),
      ui.stat({ i18n: 'history.touched', value: i18n.fmtInt(touched) })
    ]);
  }
  /* body.error is one free-text English sentence from the engine. It is rendered, not
     translated and not parsed: a sentence this UI could not have anticipated is still the
     most useful thing on the page when a run failed. */
  function errorLine(body) {
    if (!body.error) { return null; }
    return ui.el('p', { class: 'hist__error' }, [
      ui.icon('icon-caution', 'hist__error-ico'),
      ui.el('span', { class: 'hist__error-label', i18n: 'history.error_label' }),
      ui.el('span', { class: 'hist__error-text', text: String(body.error) })
    ]);
  }

  /* A real <table> with a caption and column headers, because this is tabular data and a
     screen reader should be told so rather than reading four numbers in a row. */
  function volumesTable(body) {
    var rows = body.volumes || [];
    if (!rows.length) { return null; }
    var tbody = ui.el('tbody', null, rows.map(function (v) {
      return ui.el('tr', null, [
        /* root is free text off the disk. */
        ui.el('td', { class: 'hist__td mono', text: v.root }),
        numCell(i18n.fmtBytes(v.free_before)),
        numCell(i18n.fmtBytes(v.free_after)),
        /* Signed: another process can write more than the clean removed, and "-1.2 GB"
           has to read as a loss rather than as a gain of the same size. */
        numCell(i18n.fmtBytes(v.free_delta, { signed: true }))
      ]);
    }));
    return ui.el('div', { class: 'hist__table-wrap' }, [
      ui.el('table', { class: 'hist__table hist__table--vols' }, [
        ui.el('caption', { class: 'hist__caption', i18n: 'history.volumes' }),
        ui.el('thead', null, [
          ui.el('tr', null, [
            th('history.col.volume'), th('history.col.free_before'),
            th('history.col.free_after'), th('history.col.free_delta')
          ])
        ]),
        tbody
      ])
    ]);
  }
  /* skipped_reason and locked_by are free-text English from the engine -- "locked by
     Code.exe (pid 9184)" is not a phrase this UI can compose, so it is passed through
     as-is rather than mapped to a key that would have to guess at its shape. */
  function noteCell(o) {
    var parts = [];
    if (o.skipped_reason) {
      parts.push(ui.el('span', { class: 'hist__note', text: String(o.skipped_reason) }));
    }
    if (o.locked_by) {
      parts.push(ui.el('span', { class: 'hist__note' }, [
        ui.el('span', { class: 'hist__note-label', i18n: 'history.locked_by' }),
        ui.el('span', { class: 'hist__note-value', text: String(o.locked_by) })
      ]));
    }
    if (!parts.length) { return ui.el('td', { class: 'hist__td', text: DASH }); }
    return ui.el('td', { class: 'hist__td' }, parts);
  }

  /* Engine order, not sorted: per_target is written in the order the run processed the
     targets, which is the order the log lines below it appear in. Re-sorting here would
     put the table and the console into two different orders. */
  function targetsTable(body) {
    var per = body.per_target || {};
    var ids = Object.keys(per);
    if (!ids.length) { return null; }
    var tbody = ui.el('tbody', null, ids.map(function (id) {
      var o = per[id] || {};
      return ui.el('tr', null, [
        ui.el('td', {
          class: 'hist__td hist__cell-name',
          text: displayName(o.target_id || id)
        }),
        /* reclaimed is measured, so fmtBytes -- same reasoning as detailStats(). */
        numCell(i18n.fmtBytes(o.reclaimed)),
        numCell(i18n.fmtInt(o.files_deleted)),
        numCell(i18n.fmtInt(o.files_locked)),
        numCell(i18n.fmtInt(o.denied)),
        noteCell(o)
      ]);
    }));
    return ui.el('div', { class: 'hist__table-wrap' }, [
      ui.el('table', { class: 'hist__table hist__table--targets' }, [
        ui.el('caption', { class: 'hist__caption', i18n: 'history.per_target' }),
        ui.el('thead', null, [
          ui.el('tr', null, [
            th('history.col.target'), th('history.col.reclaimed'),
            th('history.col.deleted'), th('history.col.locked'),
            th('history.col.denied'), th('history.col.note')
          ])
        ]),
        tbody
      ])
    ]);
  }
  /*
   * The right-hand pane, rebuilt from 'report' / 'detailErr' / 'selected' and nothing else
   * -- which is what makes relang() four calls and a re-selection cheap.
   *
   * The console is NOT built here. It is a sibling node mount() made once and this
   * function refills, because a fresh consolePane per selection means five thousand <p>
   * nodes per click and one more i18n.onChange listener each time.
   */
  function renderDetail() {
    var box = ui.clear(dom.detail);
    if (detailErr) {
      dom.logWrap.hidden = true;
      dom.log.clear();
      box.appendChild(ui.emptyState({
        icon: 'icon-caution',
        i18n: detailErr.code === 'no_such_report' ? 'history.gone' : 'history.detail_failed',
        text: errText(detailErr)
      }));
      return;
    }
    if (selected === null) {
      dom.logWrap.hidden = true;
      dom.log.clear();
      box.appendChild(ui.emptyState({
        icon: 'icon-info', i18n: 'history.pick', body: 'history.pick.body'
      }));
      return;
    }
    if (!report) {
      /* The gap between the click and the file read. Short, but without a word here the
         pane would still be showing the previous run's figures under a new selection. */
      dom.logWrap.hidden = true;
      box.appendChild(ui.emptyState({ icon: 'icon-refresh', i18n: 'history.loading' }));
      return;
    }
    ui.append(box, [
      detailHead(report),
      detailStats(report),
      errorLine(report),
      volumesTable(report),
      targetsTable(report)
    ]);
    /* One clear() and one push(): report events already have the {ts, level, message_vi,
       message_en} shape the pane renders, and the pane re-renders its own lines on a
       language switch, so nothing here has to translate a log line. */
    dom.log.clear();
    dom.log.push(report.events || []);
    dom.logWrap.hidden = false;
  }
  /* --- lifecycle -------------------------------------------------------------- */

  function mount(host) {
    var volsBox = ui.el('div', { class: 'hist__vols' });
    var volsCard = ui.card({
      i18n: 'history.free_now', sub: 'history.free_now.sub',
      icon: 'icon-disk', class: 'hist__vols-card'
    });
    volsCard.body.appendChild(volsBox);

    /* role="group" with a label, following ui.js's own log__filters: the children are
       buttons, so role="list" would be a lie about what they are. */
    var list = ui.el('div', {
      class: 'hist__list', role: 'group', i18nAttr: 'aria-label:history.runs'
    });
    var listCard = ui.card({
      i18n: 'history.runs', sub: 'history.runs.sub', icon: 'icon-history',
      class: 'hist__runs-card'
    });
    listCard.body.appendChild(list);

    var detail = ui.el('div', { class: 'hist__detail' });
    var log = ui.consolePane({ titleKey: 'history.events' });
    var logWrap = ui.el('div', { class: 'hist__console', hidden: true }, [log]);

    host.appendChild(ui.el('div', { class: 'hist' }, [
      ui.el('div', { class: 'hist__side' }, [volsCard, listCard]),
      ui.el('div', { class: 'hist__main' }, [detail, logWrap])
    ]));

    dom = { vols: volsBox, list: list, detail: detail, log: log, logWrap: logWrap };
    /* Draw the resting states now, so the first paint is three sentences rather than three
       blank boxes that fill in a moment later. */
    renderVols();
    renderList();
    renderDetail();
  }

  function enter(host, actions) {
    actions.appendChild(ui.btn({
      i18n: 'history.refresh', icon: 'icon-refresh', variant: 'ghost',
      on: { click: function () { load(); } }
    }));
    loadCatalog();
    load();
  }

  /* Nothing to stop. This view starts no job and holds no timer: the only live figure is
     the free-space strip, read once per enter() and once per refresh click. The function
     exists to say that out loud. */
  function leave() {
    return null;
  }
  function relang() {
    if (!dom) { return; }
    /* i18n.apply() has already retranslated every data-i18n node in the document, which
       covers the tags, the table headers, the captions and the empty states. What it
       cannot reach is every string this file formatted itself: dates, byte figures,
       durations, the two t() calls with placeholders, the stat hint, and the catalogue
       names picked in the old language. Those are redrawn from data already in hand -- no
       bridge call, because the engine's numbers did not change, only the language they
       are read in. */
    rebuildNames();
    renderVols();
    renderList();
    renderDetail();
  }

  var views = ADC.views = ADC.views || {};
  views.history = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
