/*
 * views/explorer.js -- the Disk Explorer (docs/02-SPEC.md 6.2).
 *
 * "Chọn volume hoặc thư mục → quét có tiến độ, huỷ được. Bảng top-N thư mục và top-N
 * file theo size-on-disk, đào xuống từng cấp (breadcrumb), sort/filter. Treemap đơn
 * giản bằng SVG (không thư viện ngoài)."
 *
 * Four things worth knowing before reading the rest.
 *
 * 1. This view never sees a path it can send back. `explore_start` takes a volume
 *    letter or a `node_id` the engine minted, and every row arrives WITHOUT a `path`
 *    field (src/adc/engine/explorer.py, ExploreRow.as_dict). So drilling is safe by
 *    construction: the page can only ask for a folder it was already shown. The full
 *    path of the *current* folder does arrive, and is displayed -- paths may leave,
 *    they may not enter (SEC-02).
 *
 * 2. A level is a job, and it renders while it runs. `job_poll` carries
 *    `partial_results` for an explore job from the listing phase onward, so the folder
 *    list is on screen while the sizes are still arriving. Totals, map and table all
 *    repaint on each 250 ms snapshot; a repaint that happened to steal keyboard focus
 *    would make the table unusable mid-walk, which is why renderTable() remembers
 *    which control was focused and puts focus back.
 *
 * 3. The treemap is hand-rolled and lives in a fixed 1000x440 user-space viewBox with
 *    `width: 100%; height: auto`. Areas are proportions, and a uniform scale preserves
 *    proportions, so nothing here has to measure the DOM: no getBoundingClientRect, no
 *    ResizeObserver, no dependence on layout having settled. The algorithm is
 *    Bruls/Huizing/van Wijk squarified layout, ~30 lines below.
 *
 * 4. One walk at a time. The engine enforces it (`busy`), and this view asks
 *    `app().claimJob('explore')` BEFORE calling the bridge so two clicks cannot race.
 *    `leave()` stops polling but neither cancels the job nor drops the claim: a walk
 *    the user started keeps running while they look at another view, and `enter()`
 *    re-attaches to it instead of starting a second one.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;

  /* index.html loads the views before app.js, so ADC.app is resolved per call. */
  function app() { return ADC.app; }

  var DASH = '—';

  /* ui.js keeps SVG_NS to itself and ui.el() builds HTML elements, so this file needs
     its own namespace constant and its own tiny createElementNS helper. */
  var SVG_NS = 'http://www.w3.org/2000/svg';

  /* Treemap user space. 1000x440 is a hair wider than 2:1, which is the shape the
     content column gives it at every window width the app supports. */
  var MAP_W = 1000;
  var MAP_H = 440;
  /* Tiles are capped so a folder with 80 children does not turn into 80 slivers; the
     surplus is folded into the "everything else" tile rather than dropped, so the map
     always accounts for the whole folder. */
  var MAP_TILES = 36;
  /* A tile below this share of the total would be under a pixel wide at any realistic
     render size. Folding it in keeps the map free of invisible geometry. */
  var MAP_MIN_SHARE = 0.0008;
  var MAP_GAP = 2;

  /* A label needs room for itself and for the size under it, in user units. */
  var LABEL_MIN_W = 108;
  var LABEL_MIN_H = 44;
  /* Advance width of one character of --font-ui at the tile label size, measured in
     WebView2 148 and rounded up. Only used to decide where to cut a name, so an
     approximation is the right tool: being one character conservative is invisible,
     and measuring text would mean touching layout (see note 3 in the header). */
  var LABEL_CHAR = 8.4;

  /* --- small helpers ---------------------------------------------------------- */

  function nz(value) {
    return typeof value === 'number' && isFinite(value) ? value : 0;
  }

  function errText(err) {
    if (!err) { return ''; }
    if (typeof err.text === 'function') { return err.text(); }
    return String(err.message || err);
  }

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (var key in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, key)) {
          node.setAttribute(key, String(attrs[key]));
        }
      }
    }
    return node;
  }

  /* --- columns ---------------------------------------------------------------- */

  /*
   * The table's shape, once, because the header cells and the body cells have to agree
   * about order and alignment and there is no reason for that agreement to be implicit.
   *
   * `share` is not sortable: it is size divided by a constant, so sorting by it is
   * sorting by size and a second control for the same order would be a lie. `actions`
   * carries a real visible header rather than a hidden one, because this stylesheet has
   * no visually-hidden utility and inventing one for a single cell is not worth it.
   */
  var COLS = [
    { key: 'name', label: 'explorer.col.name', sortable: true, numeric: false },
    { key: 'size', label: 'explorer.col.size', sortable: true, numeric: true },
    { key: 'share', label: 'explorer.col.share', sortable: false, numeric: true },
    { key: 'items', label: 'explorer.col.items', sortable: true, numeric: true },
    { key: 'mtime', label: 'explorer.col.mtime', sortable: true, numeric: true },
    { key: 'actions', label: 'explorer.col.actions', sortable: false, numeric: false }
  ];

  var SORTS = {
    name: function (a, b) { return String(a.name).localeCompare(String(b.name)); },
    size: function (a, b) { return nz(a.size) - nz(b.size); },
    items: function (a, b) {
      return (nz(a.dirs) + nz(a.files)) - (nz(b.dirs) + nz(b.files));
    },
    mtime: function (a, b) { return nz(a.mtime) - nz(b.mtime); }
  };

  /* --- state ------------------------------------------------------------------ */

  var dom = null;

  /* The drive strip. `volsSeq` is a generation counter: a reply from a superseded
     request must not repaint over a newer one. */
  var vols = null;
  var volsError = null;
  var volsSeq = 0;

  /* `head` is the level's identity -- root, name, volume, node_id, parent_id, crumbs --
     and it arrives from explore_start's immediate reply, before the first poll, so the
     breadcrumb bar draws at once instead of after a 250 ms tick. `level` is the last
     partial_results payload: rows and totals, growing while the walk runs. */
  var head = null;
  var level = null;
  var levelSeq = 0;
  var outcome = null;

  var jobId = null;
  var watcher = null;
  var release = null;
  var running = false;
  var lastPct = 0;

  var sortKey = 'size';
  var sortAsc = false;
  var filterText = '';

  /* --- the walk --------------------------------------------------------------- */

  function openVolume(letter) { start(letter, null); }

  function openNode(nodeId) { if (nodeId) { start(null, nodeId); } }

  /*
   * Start a level. Exactly one of the two handles is passed -- the bridge refuses both
   * and refuses neither -- and neither of them is a path.
   *
   * The claim comes first, before the bridge call, because a user who double-clicks a
   * folder would otherwise get two walks racing for the same engine slot and a `busy`
   * error for the one that lost. Asking app() first turns that into a toast.
   */
  function start(volumeId, nodeId) {
    if (running) { return; }
    var claim = app().claimJob('explore');
    if (!claim) {
      var busy = app().busyWith();
      var known = busy === 'scan' || busy === 'clean' || busy === 'explore';
      ui.toast(i18n.t('explorer.busy', {
        kind: i18n.t('kind.' + (known ? busy : 'unknown'))
      }), { kind: 'warn' });
      return;
    }
    release = claim;

    var seq = ++levelSeq;
    outcome = null;
    level = null;
    lastPct = 0;
    setRunning(true);
    dom.progress.set(0, i18n.t('explorer.starting'));
    renderAll();

    api.exploreStart(volumeId, nodeId).then(function (started) {
      if (seq !== levelSeq) { return; }
      head = {
        root: started.root,
        name: started.name,
        volume: started.volume,
        node_id: started.node_id,
        parent_id: started.parent_id,
        crumbs: started.crumbs || []
      };
      /* A new level is a new list; carrying the old filter over would hide rows the
         user never filtered. Sort order is a preference and does carry over. */
      filterText = '';
      if (dom.filter) { dom.filter.value = ''; }
      renderAll();
      attach(started.job_id);
    }, function (err) {
      if (seq !== levelSeq) { return; }
      finishRun();
      renderAll();
      ui.showError(err);
    });
  }

  function attach(id) {
    jobId = id;
    setRunning(true);
    watcher = api.watch(id, { onSnapshot: onSnapshot });
    watcher.promise.then(onSettled, onWatchFailed);
  }

  /*
   * One poll. `partial_results` for an explore job is the level itself -- the same
   * dict `ExploreRun.as_dict()` returns -- so a snapshot is all this view needs.
   *
   * The identity is taken from the snapshot when it disagrees with what is on screen,
   * which matters on re-attach: a walk started before the user wandered off to another
   * view comes back with its crumbs intact even though enter() knows only the job id.
   */
  function onSnapshot(snap) {
    if (!dom) { return; }
    lastPct = typeof snap.pct === 'number' ? snap.pct * 100 : 0;
    dom.progress.set(lastPct, snap.cancel_requested
      ? i18n.t('explorer.stopping')
      : i18n.pickField(snap, 'detail'));

    var partial = snap.partial_results;
    if (!partial || partial.kind !== 'explore') { return; }
    level = partial;
    if (!head || head.node_id !== partial.node_id) {
      head = {
        root: partial.root,
        name: partial.name,
        volume: partial.volume,
        node_id: partial.node_id,
        parent_id: partial.parent_id,
        crumbs: partial.crumbs || []
      };
    }
    renderAll();
  }

  /* Release the claim exactly once, on every exit path. Two releases would let a
     second walk start while the first is still winding down. */
  function finishRun() {
    if (release) {
      var done = release;
      release = null;
      done();
    }
    watcher = null;
    jobId = null;
    setRunning(false);
  }

  /*
   * The job ended. `cancelled` and `failed` are outcomes to render, not errors to
   * throw: a cancelled walk still measured something and those figures are worth
   * showing, flagged as partial.
   */
  function onSettled(snap) {
    finishRun();
    var final = snap || {};
    if (final.partial_results && final.partial_results.kind === 'explore') {
      level = final.partial_results;
    }
    outcome = final.state === 'cancelled' || final.state === 'failed' ? final.state : null;
    renderAll();
    if (outcome === 'cancelled') {
      ui.toast(i18n.t('explorer.cancelled'), { kind: 'info' });
    } else if (outcome === 'failed') {
      ui.toast(i18n.t('explorer.failed'), { kind: 'error' });
    }
  }

  /*
   * Polling itself became impossible. `no_such_job` is the ordinary case and not
   * really a failure: the engine forgot a finished job while this view was on another
   * screen, so there is nothing to recover and nothing to alarm anyone about.
   */
  function onWatchFailed(err) {
    finishRun();
    renderAll();
    if (err && err.code === 'no_such_job') {
      ui.toast(i18n.t('explorer.lost'), { kind: 'info' });
      return;
    }
    ui.showError(err);
  }

  function stopWalk() {
    if (!watcher) { return; }
    /* Disable the button, not the walk: the engine checks its cancel flag between
       entries, so the job settles on its own and finishRun() does the rest. */
    dom.stop.disabled = true;
    dom.progress.set(lastPct, i18n.t('explorer.stopping'));
    watcher.cancel();
  }

  function setRunning(on) {
    running = on;
    if (!dom) { return; }
    dom.stop.hidden = !on;
    dom.stop.disabled = false;
    dom.progress.hidden = !on;
  }

  function reveal(nodeId) {
    if (!nodeId) { return; }
    /* The only method that answers with a path. It opens the folder the id names --
       and for a file's id, its parent, because os.startfile on a file would run it. */
    api.exploreReveal(nodeId).then(function (res) {
      ui.toast(i18n.t('explorer.revealed', { path: (res && res.opened) || '' }), {
        kind: 'success'
      });
    }, function (err) { ui.showError(err); });
  }

  /* --- drives ----------------------------------------------------------------- */

  function loadVolumes(force) {
    var seq = ++volsSeq;
    volsError = null;
    app().volumes(force).then(function (payload) {
      if (seq !== volsSeq) { return; }
      vols = (payload && payload.volumes) || [];
      renderVols();
    }, function (err) {
      if (seq !== volsSeq) { return; }
      vols = null;
      volsError = err;
      renderVols();
    });
  }

  function retryBtn(fn) {
    return ui.btn({ i18n: 'explorer.retry', variant: 'ghost', size: 'sm', on: { click: fn } });
  }

  function volButton(vol) {
    var letter = vol.letter;
    var node = ui.el('button', {
      class: 'dx__vol',
      type: 'button',
      disabled: running,
      on: { click: function () { openVolume(letter); } }
    }, [
      ui.icon('icon-disk', 'dx__vol-ico'),
      ui.el('span', { class: 'dx__vol-body' }, [
        ui.el('span', {
          class: 'dx__vol-name',
          text: vol.display_name || vol.root || String(letter || '')
        }),
        ui.el('span', {
          class: 'dx__vol-meta num',
          text: i18n.t('explorer.vol.free', {
            free: i18n.fmtBytes(vol.free),
            total: i18n.fmtBytes(vol.total)
          })
        })
      ])
    ]);
    /* The drive whose tree is on screen. aria-current says it out loud, because a
       tinted border is not information. */
    if (head && head.volume === letter) {
      node.classList.add('is-active');
      node.setAttribute('aria-current', 'true');
    }
    return node;
  }

  /*
   * Three states, and they are not the same thing: a failed call gets an error and a
   * retry, a call still in flight gets nothing (the panel is one paint away), and an
   * empty list gets told so.
   */
  function renderVols() {
    if (!dom) { return; }
    ui.clear(dom.vols);
    if (volsError) {
      dom.vols.appendChild(ui.emptyState({
        icon: 'icon-caution',
        i18n: 'explorer.vol.error',
        text: errText(volsError),
        action: retryBtn(function () { loadVolumes(true); })
      }));
      return;
    }
    if (vols === null) { return; }
    if (!vols.length) {
      dom.vols.appendChild(ui.emptyState({ icon: 'icon-disk', i18n: 'explorer.vol.none' }));
      return;
    }
    for (var i = 0; i < vols.length; i += 1) { dom.vols.appendChild(volButton(vols[i])); }
  }

  /* --- breadcrumbs ------------------------------------------------------------ */

  /*
   * Every crumb is a fresh handle the engine minted for this level, so climbing back
   * up is the same call as drilling down and the page still never names a path.
   *
   * `parent_id === null` is how the engine says "this is a volume root", which is what
   * disables Up -- the alternative would be parsing a path this view was never given.
   */
  function crumbButton(crumb, here) {
    var nodeId = crumb.node_id;
    var node = ui.el('button', {
      class: here ? 'dx__crumb dx__crumb--here' : 'dx__crumb',
      type: 'button',
      disabled: running || here,
      text: crumb.label,
      on: { click: function () { openNode(nodeId); } }
    });
    if (here) { node.setAttribute('aria-current', 'page'); }
    return node;
  }

  function renderCrumbs() {
    if (!dom) { return; }
    ui.clear(dom.crumbs);
    if (!head) { return; }
    var upId = head.parent_id;
    dom.crumbs.appendChild(ui.btn({
      icon: 'icon-chevron',
      label: 'explorer.up',
      variant: 'ghost',
      size: 'sm',
      class: 'dx__up',
      disabled: running || !upId,
      on: { click: function () { openNode(upId); } }
    }));
    var crumbs = head.crumbs || [];
    for (var i = 0; i < crumbs.length; i += 1) {
      if (i > 0) { dom.crumbs.appendChild(ui.icon('icon-chevron', 'dx__crumb-sep')); }
      dom.crumbs.appendChild(crumbButton(crumbs[i], i === crumbs.length - 1));
    }
  }

  function renderPath() {
    if (!dom) { return; }
    dom.path.textContent = head ? String(head.root || '') : '';
    dom.path.hidden = !head;
    dom.reload.disabled = running || !head;
    dom.revealHere.disabled = !head;
  }

  /* --- totals ----------------------------------------------------------------- */

  /*
   * `truncated` on the totals means the level hit its time budget, so every figure
   * derived from it is a lower bound. fmtBytes({truncated: true}) is what puts the
   * "≥" in front, and the same flag drives the note under the table.
   */
  function renderTotals() {
    if (!dom) { return; }
    ui.clear(dom.totals);
    var t = level && level.totals;
    if (!t) { return; }
    var lower = t.truncated === true;
    ui.append(dom.totals, [
      ui.stat({
        value: i18n.fmtBytes(t.total_size, { truncated: lower }),
        i18n: 'explorer.total',
        tone: lower ? 'warn' : null,
        hint: lower ? i18n.t('explorer.total.partial') : null
      }),
      ui.stat({ value: i18n.fmtInt(t.children), i18n: 'explorer.children' }),
      ui.stat({ value: i18n.fmtInt(t.dirs), i18n: 'explorer.dirs' }),
      ui.stat({ value: i18n.fmtInt(t.files), i18n: 'explorer.files' }),
      ui.stat({
        value: i18n.fmtDuration(t.elapsed_s),
        i18n: 'explorer.elapsed',
        hint: nz(t.to_measure) > 0 ? i18n.t('explorer.measured', {
          done: i18n.fmtInt(t.measured),
          all: i18n.fmtInt(t.to_measure)
        }) : null
      })
    ]);
  }

  /* --- treemap ---------------------------------------------------------------- */

  /*
   * What goes on the map, and the one invariant it keeps: the tiles account for the
   * whole folder. Rows past the cap and rows too small to draw are not dropped, they
   * are added to the remainder tile -- which starts from `other_size`, the engine's own
   * "measured but not shown" figure, so the map cannot quietly disagree with the total.
   */
  /* The tile vocabulary is closed: four classes, four rules, nothing built from an
     engine string that might one day gain a fifth member. */
  function tileKind(kind) {
    if (kind === 'dir') { return 'dir'; }
    if (kind === 'link') { return 'link'; }
    return 'file';
  }

  function mapItems() {
    var t = (level && level.totals) || {};
    var rows = (level && level.rows) || [];
    var total = nz(t.total_size);
    var out = [];
    var covered = 0;
    for (var i = 0; i < rows.length && out.length < MAP_TILES; i += 1) {
      var value = nz(rows[i].size);
      if (value <= 0) { continue; }
      if (total > 0 && value / total < MAP_MIN_SHARE) { continue; }
      out.push({
        row: rows[i],
        name: String(rows[i].name),
        kind: tileKind(rows[i].kind),
        value: value
      });
      covered += value;
    }
    if (!out.length) { return out; }
    var rest = Math.max(0, total - covered);
    if (total > 0 && rest / total >= MAP_MIN_SHARE) {
      out.push({ row: null, name: i18n.t('explorer.map.other'), kind: 'other', value: rest });
    }
    return out;
  }

  /*
   * The worst aspect ratio a row of tiles would have if it were laid along `side` with
   * total area `sum`. One tile of area `a` in a strip of thickness `sum / side` is
   * `(sum / side)` by `(a * side / sum)`, so its aspect is
   *
   *     max( sum² / (a · side²) ,  a · side² / sum² )
   *
   * and the worst tile in the row is the smallest or the largest one -- hence only
   * those two need testing. This is Bruls/Huizing/van Wijk, "Squarified Treemaps".
   */
  function worst(sum, side, minValue, maxValue) {
    var s2 = sum * sum;
    var side2 = side * side;
    return Math.max(s2 / (minValue * side2), (maxValue * side2) / s2);
  }

  /*
   * Lay `items` (each with an `area`, biggest first) into the rectangle x/y/w/h.
   *
   * Rows grow while adding the next tile makes the worst aspect ratio better, and are
   * flushed the moment it makes it worse; the strip always runs along the shorter side,
   * which is what keeps tiles near-square instead of degenerating into ribbons.
   */
  function squarify(items, x, y, w, h) {
    var placed = [];
    var start = 0;
    while (start < items.length && w > 0.5 && h > 0.5) {
      var side = Math.min(w, h);
      var sum = items[start].area;
      var best = worst(sum, side, items[start].area, items[start].area);
      var end = start + 1;
      while (end < items.length) {
        var grown = sum + items[end].area;
        /* items are sorted descending, so the row's min is the newcomer and its max is
           the tile that opened the row. */
        var candidate = worst(grown, side, items[end].area, items[start].area);
        if (candidate > best) { break; }
        best = candidate;
        sum = grown;
        end += 1;
      }
      var thick = sum / side;
      var along = 0;
      for (var i = start; i < end; i += 1) {
        var span = items[i].area / thick;
        if (w >= h) {
          placed.push({ item: items[i], x: x, y: y + along, w: thick, h: span });
        } else {
          placed.push({ item: items[i], x: x + along, y: y, w: span, h: thick });
        }
        along += span;
      }
      if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
      start = end;
    }
    return placed;
  }

  function layoutMap(items) {
    var total = 0;
    var i;
    for (i = 0; i < items.length; i += 1) { total += items[i].value; }
    if (total <= 0) { return []; }
    /* Areas, not sizes: scaled so the tiles fill the viewBox exactly. Everything after
       this point is geometry in user units and knows nothing about bytes. */
    var scale = (MAP_W * MAP_H) / total;
    for (i = 0; i < items.length; i += 1) { items[i].area = items[i].value * scale; }
    /* Rows arrive biggest-first, but the remainder tile is appended last and can be
       larger than the tail, and squarify() requires descending order. */
    items.sort(function (a, b) { return b.area - a.area; });
    return squarify(items, 0, 0, MAP_W, MAP_H);
  }

  /* Cut a name to what fits, rather than letting SVG text run over its neighbour --
     SVG has no overflow clipping to fall back on. */
  function clip(text, width) {
    var room = Math.floor((width - 12) / LABEL_CHAR);
    if (room < 4) { return ''; }
    var name = String(text);
    return name.length <= room ? name : name.slice(0, room - 1) + '…';
  }

  function tile(box, total) {
    var item = box.item;
    var drillable = item.row && item.row.kind === 'dir';
    var classes = ['dx__tile', 'dx__tile--' + item.kind];
    if (drillable) { classes.push('dx__tile--open'); }
    var group = svgEl('g', { class: classes.join(' ') });

    var share = total > 0 ? (item.value / total) * 100 : 0;
    /* A native SVG tooltip. The map is one aria leaf (see renderMap), so this is a
       pointer affordance only -- the table below carries the same figures for keyboard
       and screen-reader users. */
    var tip = svgEl('title');
    tip.textContent = i18n.t('explorer.map.tip', {
      name: item.name,
      size: i18n.fmtBytes(item.value),
      pct: i18n.fmtPct(share)
    });
    group.appendChild(tip);

    var x = box.x + MAP_GAP;
    var y = box.y + MAP_GAP;
    var w = Math.max(0, box.w - MAP_GAP);
    var h = Math.max(0, box.h - MAP_GAP);
    group.appendChild(svgEl('rect', {
      class: 'dx__tile-box', x: x, y: y, width: w, height: h, rx: 3
    }));

    if (w >= LABEL_MIN_W && h >= LABEL_MIN_H) {
      var label = clip(item.name, w);
      if (label) {
        var name = svgEl('text', { class: 'dx__tile-name', x: x + 6, y: y + 20 });
        name.textContent = label;
        group.appendChild(name);
        var size = svgEl('text', { class: 'dx__tile-size', x: x + 6, y: y + 37 });
        size.textContent = i18n.fmtBytes(item.value, { truncated: item.row
          ? item.row.truncated === true : false });
        group.appendChild(size);
      }
    }

    if (drillable) {
      var nodeId = item.row.node_id;
      group.addEventListener('click', function () {
        if (!running) { openNode(nodeId); }
      });
    }
    return group;
  }

  /*
   * The map, as one image.
   *
   * `role="img"` with a label is deliberate: it makes the whole SVG a single leaf for
   * assistive technology instead of 37 unlabelled shapes, and the table underneath is
   * the accessible equivalent -- same rows, same figures, real buttons, reachable by
   * keyboard. Nothing here is the only way to do anything.
   */
  function renderMap() {
    if (!dom) { return; }
    ui.clear(dom.map);
    if (!level) { return; }
    var items = mapItems();
    if (!items.length) {
      dom.map.appendChild(ui.el('p', { class: 'dx__map-empty', i18n: 'explorer.map.empty' }));
      return;
    }
    var boxes = layoutMap(items);
    var t = level.totals || {};
    var total = nz(t.total_size);
    var canvas = svgEl('svg', {
      class: 'dx__map-svg',
      viewBox: '0 0 ' + MAP_W + ' ' + MAP_H,
      /* Areas survive a non-uniform scale -- both axes multiply into every tile, so
         every tile changes by the same factor and the proportions hold. Only the
         squareness would suffer, and only if something ever forced a box that is not
         1000:440, which `height: auto` does not. */
      preserveAspectRatio: 'none',
      role: 'img'
    });
    canvas.setAttribute('aria-label', i18n.t('explorer.map.label', {
      count: i18n.fmtInt(boxes.length),
      size: i18n.fmtBytes(total, { truncated: t.truncated === true })
    }));
    for (var i = 0; i < boxes.length; i += 1) {
      canvas.appendChild(tile(boxes[i], total));
    }
    dom.map.appendChild(canvas);
  }

  /* --- table ------------------------------------------------------------------ */

  function visibleRows() {
    var rows = (level && level.rows) || [];
    var needle = filterText.trim().toLowerCase();
    var out = [];
    for (var i = 0; i < rows.length; i += 1) {
      if (!needle || String(rows[i].name).toLowerCase().indexOf(needle) !== -1) {
        out.push(rows[i]);
      }
    }
    var cmp = SORTS[sortKey] || SORTS.size;
    out.sort(function (a, b) {
      var delta = cmp(a, b);
      return sortAsc ? delta : -delta;
    });
    return out;
  }

  function sortBy(key, numeric) {
    if (sortKey === key) {
      sortAsc = !sortAsc;
    } else {
      sortKey = key;
      /* Names read best A→Z, numbers best biggest-first. */
      sortAsc = !numeric;
    }
    renderTable();
  }

  /*
   * A sortable column header: a real <button> inside the <th>, with `aria-sort` on the
   * th itself. The button is what makes the sort reachable by keyboard; aria-sort is
   * what tells a screen reader the table is ordered and which way.
   */
  function headCell(col) {
    var active = col.sortable && sortKey === col.key;
    var classes = ['dx__th'];
    if (col.numeric) { classes.push('dx__th--num'); }
    if (active) { classes.push('dx__th--sorted'); }
    var cell = ui.el('th', { class: classes.join(' '), attrs: { scope: 'col' } });
    if (!col.sortable) {
      cell.appendChild(ui.el('span', { i18n: col.label }));
      return cell;
    }
    cell.setAttribute('aria-sort', active ? (sortAsc ? 'ascending' : 'descending') : 'none');
    var button = ui.el('button', {
      class: 'dx__sort',
      type: 'button',
      attrs: {
        'data-dx': 'h-' + col.key,
        'aria-label': i18n.t('explorer.sort', { col: i18n.t(col.label) })
      },
      on: { click: function () { sortBy(col.key, col.numeric); } }
    }, [ui.el('span', { i18n: col.label })]);
    /* Two calls rather than one with a computed class, so the stylesheet audit can see
       both arrows and prove both have a rule. */
    if (active && sortAsc) { button.appendChild(ui.icon('icon-chevron', 'dx__arrow dx__arrow--up')); }
    if (active && !sortAsc) { button.appendChild(ui.icon('icon-chevron', 'dx__arrow dx__arrow--down')); }
    cell.appendChild(button);
    return cell;
  }

  /* No file glyph was added to the sprite: icon-log is already a document, and a second
     near-identical symbol would be weight for nothing. icon-link is new -- a reparse
     point is a thing this UI had no way to draw before. */
  function kindIcon(row) {
    if (row.kind === 'dir') { return ui.icon('icon-folder', 'dx__row-ico'); }
    if (row.kind === 'link') { return ui.icon('icon-link', 'dx__row-ico'); }
    return ui.icon('icon-log', 'dx__row-ico');
  }

  function itemsText(row) {
    if (row.kind !== 'dir') { return DASH; }
    return i18n.t('explorer.items', {
      dirs: i18n.fmtInt(nz(row.dirs)),
      files: i18n.fmtInt(nz(row.files))
    });
  }

  /*
   * Why a row's number might not be what it looks like. Every one of these is a fact
   * the engine sent, and every one of them changes how the size should be read -- a
   * junction is not sized at all, a divergent folder is compressed or sparse, a denied
   * subtree is missing from the sum.
   */
  function rowTags(row) {
    var tags = [];
    if (row.kind === 'link') {
      tags.push(ui.tag({
        tone: 'info', icon: 'icon-link',
        i18n: 'explorer.tag.link', title: 'explorer.tag.link.help'
      }));
    }
    if (row.divergent === true) {
      tags.push(ui.tag({
        tone: 'info',
        i18n: 'explorer.tag.divergent', title: 'explorer.tag.divergent.help'
      }));
    }
    if (nz(row.denied_count) > 0) {
      tags.push(ui.tag({
        tone: 'warn', icon: 'icon-lock',
        text: i18n.t('explorer.tag.denied', { n: i18n.fmtInt(row.denied_count) })
      }));
    }
    if (row.error) {
      /* The engine's own wording, already localised, rather than a code this view
         would have to keep a table for. */
      tags.push(ui.tag({ tone: 'danger', icon: 'icon-caution', text: String(row.error) }));
    }
    if (!tags.length) { return null; }
    return ui.el('span', { class: 'dx__row-tags' }, tags);
  }

  /*
   * Drill is offered for folders only. A file's handle is refused by `explore_start`
   * with `bad_input`, and a link's is deliberately not followed: a junction can point
   * at its own ancestor and walking one is how you measure a directory tree forever.
   * Reveal is offered for everything and stays enabled during a walk -- it opens a
   * window, it does not touch the job.
   */
  function rowActions(row) {
    var out = [];
    var nodeId = row.node_id;
    if (row.kind === 'dir') {
      var drill = ui.btn({
        icon: 'icon-chevron',
        label: 'explorer.row.open',
        variant: 'bare',
        size: 'sm',
        disabled: running,
        on: { click: function () { openNode(nodeId); } }
      });
      drill.setAttribute('data-dx', 'o-' + nodeId);
      out.push(drill);
    }
    var open = ui.btn({
      icon: 'icon-explorer',
      label: 'explorer.row.reveal',
      variant: 'bare',
      size: 'sm',
      on: { click: function () { reveal(nodeId); } }
    });
    open.setAttribute('data-dx', 'r-' + nodeId);
    out.push(open);
    return out;
  }

  function rowNode(row, total) {
    var share = total > 0 ? (nz(row.size) / total) * 100 : 0;
    return ui.el('tr', { class: 'dx__tr' }, [
      ui.el('td', { class: 'dx__td dx__td--name' }, [
        kindIcon(row),
        ui.el('span', { class: 'dx__name', text: String(row.name) }),
        rowTags(row)
      ]),
      /* ui.size() takes the row, not the number, so `truncated` keeps its "≥" and its
         explanatory tooltip instead of being flattened into a plain figure. */
      ui.el('td', { class: 'dx__td num' }, [ui.size(row)]),
      ui.el('td', { class: 'dx__td num', text: total > 0 ? i18n.fmtPct(share) : DASH }),
      ui.el('td', { class: 'dx__td num', text: itemsText(row) }),
      ui.el('td', { class: 'dx__td num', text: row.mtime ? i18n.fmtDate(row.mtime) : DASH }),
      ui.el('td', { class: 'dx__td dx__td--act' }, rowActions(row))
    ]);
  }

  /*
   * Focus survives a repaint.
   *
   * The table is rebuilt on every 250 ms snapshot while a walk runs. Rebuilding the
   * subtree that holds the focused element moves focus to <body>, which for a keyboard
   * user means the table cannot be used at all until the walk ends. So the focused
   * control is identified by a stable key -- the column, or the node id plus which of
   * its two buttons -- and focus is put back on the equivalent control afterwards.
   *
   * The key is checked against a pattern before it reaches querySelector because it is
   * built from engine data; node ids are hex, but "it cannot contain a quote" is a
   * property worth asserting rather than assuming.
   */
  var FOCUS_KEY = /^[a-z]-[a-z0-9_]+$/;

  function focusKey() {
    var active = document.activeElement;
    if (!active || !dom.tableWrap.contains(active)) { return null; }
    var key = active.getAttribute('data-dx');
    return key && FOCUS_KEY.test(key) ? key : null;
  }

  function restoreFocus(key) {
    if (!key) { return; }
    var target = dom.tableWrap.querySelector('[data-dx="' + key + '"]');
    if (target && !target.disabled) { target.focus(); }
  }

  function renderTable() {
    if (!dom) { return; }
    var keep = focusKey();
    var rows = visibleRows();
    var all = ((level && level.rows) || []).length;
    var total = nz(level && level.totals && level.totals.total_size);

    ui.clear(dom.thead);
    var headRow = ui.el('tr', null, null);
    for (var c = 0; c < COLS.length; c += 1) { headRow.appendChild(headCell(COLS[c])); }
    dom.thead.appendChild(headRow);

    ui.clear(dom.tbody);
    for (var i = 0; i < rows.length; i += 1) {
      dom.tbody.appendChild(rowNode(rows[i], total));
    }

    dom.count.textContent = all
      ? i18n.t('explorer.showing', {
        shown: i18n.fmtInt(rows.length),
        all: i18n.fmtInt(all)
      })
      : '';
    dom.tableWrap.hidden = !rows.length;
    ui.clear(dom.tableEmpty);
    if (!rows.length && level) {
      dom.tableEmpty.appendChild(ui.emptyState({
        icon: 'icon-folder',
        i18n: all ? 'explorer.rows.filtered' : 'explorer.rows.none'
      }));
    }
    restoreFocus(keep);
  }

  /* --- notes ------------------------------------------------------------------- */

  function noteRow(name, child) {
    return ui.el('p', { class: 'dx__note' }, [ui.icon(name, 'dx__note-ico'), child]);
  }

  function note(name, key) {
    return noteRow(name, ui.el('span', { i18n: key }));
  }

  function noteText(name, text) {
    return noteRow(name, ui.el('span', { text: text }));
  }

  /*
   * Everything that qualifies the numbers above, stated in words under them.
   *
   * A level that ran out of budget, a denied subtree, a run measuring logical size
   * instead of size on disk: each of these makes the total mean something slightly
   * different, and none of them is visible from the figure itself.
   */
  function renderNotes() {
    if (!dom) { return; }
    ui.clear(dom.notes);
    var t = level && level.totals;
    if (!t) { return; }
    var out = [];
    if (t.truncated === true) { out.push(note('icon-caution', 'explorer.note.truncated')); }
    if (t.measured_on_disk === false) { out.push(note('icon-info', 'explorer.note.logical')); }
    if (nz(t.omitted) > 0) {
      out.push(noteText('icon-info', i18n.t('explorer.note.omitted', {
        n: i18n.fmtInt(t.omitted)
      })));
    }
    if (nz(t.denied_count) > 0) {
      out.push(noteText('icon-lock', i18n.t('explorer.note.denied', {
        n: i18n.fmtInt(t.denied_count)
      })));
    }
    if (t.error) { out.push(noteText('icon-dangerous', String(t.error))); }
    if (outcome === 'cancelled') { out.push(note('icon-stop', 'explorer.note.cancelled')); }
    ui.append(dom.notes, out);
  }

  function renderAll() {
    if (!dom) { return; }
    renderVols();
    renderCrumbs();
    renderPath();
    renderTotals();
    renderMap();
    renderTable();
    renderNotes();
    dom.body.hidden = !head;
    dom.empty.hidden = !!head;
  }

  /* --- lifecycle -------------------------------------------------------------- */

  function reloadLevel() {
    if (head && head.node_id) { openNode(head.node_id); }
  }

  function revealCurrent() {
    if (head && head.node_id) { reveal(head.node_id); }
  }

  function mount(host) {
    var pick = ui.card({ icon: 'icon-disk', i18n: 'explorer.pick', sub: 'explorer.pick.help' });
    var volsBox = ui.el('div', { class: 'dx__vols' });
    pick.body.appendChild(volsBox);

    var stop = ui.btn({
      icon: 'icon-stop', i18n: 'explorer.stop', variant: 'danger', size: 'sm',
      on: { click: stopWalk }
    });
    stop.hidden = true;
    var reload = ui.btn({
      icon: 'icon-refresh', label: 'explorer.reload', variant: 'ghost', size: 'sm',
      disabled: true, on: { click: reloadLevel }
    });
    var revealHere = ui.btn({
      icon: 'icon-explorer', label: 'explorer.reveal', variant: 'ghost', size: 'sm',
      disabled: true, on: { click: revealCurrent }
    });
    var panel = ui.card({
      icon: 'icon-explorer', i18n: 'explorer.level', sub: 'explorer.level.sub',
      actions: [reload, revealHere, stop]
    });

    var crumbs = ui.el('nav', {
      class: 'dx__crumbs', i18nAttr: 'aria-label:explorer.crumbs'
    });
    var path = ui.el('p', { class: 'dx__path mono' });
    var progress = ui.progress();
    progress.hidden = true;

    var totals = ui.el('div', { class: 'dx__totals' });
    var map = ui.el('div', { class: 'dx__map' });
    var filter = ui.el('input', {
      type: 'search',
      class: 'dx__filter',
      i18nAttr: 'placeholder:explorer.filter.hint',
      on: {
        input: function (ev) {
          filterText = ev.target.value;
          renderTable();
        }
      }
    });
    var count = ui.el('p', { class: 'dx__count num' });
    var tools = ui.el('div', { class: 'dx__tools' }, [
      ui.field({ i18n: 'explorer.filter', control: filter, id: 'dx-filter' }),
      count
    ]);

    var thead = ui.el('thead', null, null);
    var tbody = ui.el('tbody', null, null);
    /* A real table with a caption and column headers, because this is tabular data and
       a screen reader should be told so -- and because it is the map's equivalent. */
    var tableWrap = ui.el('div', { class: 'dx__table-wrap' }, [
      ui.el('table', { class: 'dx__table' }, [
        ui.el('caption', { class: 'dx__caption', i18n: 'explorer.table' }),
        thead,
        tbody
      ])
    ]);
    tableWrap.hidden = true;
    var tableEmpty = ui.el('div', { class: 'dx__table-empty' });
    var notes = ui.el('div', { class: 'dx__notes' });

    var body = ui.el('div', { class: 'dx__body' }, [
      totals, map, tools, tableWrap, tableEmpty, notes
    ]);
    body.hidden = true;
    var empty = ui.emptyState({
      icon: 'icon-explorer', i18n: 'explorer.empty', body: 'explorer.empty.body'
    });
    ui.append(panel.body, [crumbs, path, progress, body, empty]);

    host.appendChild(pick);
    host.appendChild(panel);

    dom = {
      vols: volsBox,
      crumbs: crumbs,
      path: path,
      progress: progress,
      stop: stop,
      reload: reload,
      revealHere: revealHere,
      totals: totals,
      map: map,
      filter: filter,
      count: count,
      thead: thead,
      tbody: tbody,
      tableWrap: tableWrap,
      tableEmpty: tableEmpty,
      notes: notes,
      body: body,
      empty: empty
    };
  }

  /*
   * Per visit. The drive list is refreshed because free space moves -- a clean may have
   * run since -- and a walk left running while the user was elsewhere is re-attached
   * rather than restarted: `jobId` outlives `watcher`, and that is the difference
   * between picking the polling back up and walking a million files twice.
   */
  function enter(host, actions) {
    if (actions) {
      actions.appendChild(ui.btn({
        icon: 'icon-refresh', label: 'explorer.refresh', variant: 'ghost',
        on: { click: function () { loadVolumes(true); } }
      }));
    }
    loadVolumes(true);
    if (jobId && !watcher) { attach(jobId); }
    renderAll();
  }

  /* Stop polling, keep the job. Cancelling here would throw away a walk the user only
     navigated away from, and releasing the claim would let a second one start behind
     the first. Both are deliberate omissions. */
  function leave() {
    if (!watcher) { return; }
    watcher.stopPolling();
    watcher = null;
  }

  /* Static text re-langs itself through data-i18n; everything built from engine data
     has to be rebuilt, because "12,3 GB" and "12.3 GB" are different strings. */
  function relang() {
    renderAll();
  }

  views.explorer = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
