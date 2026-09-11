/*
 * views/vss.js -- ADC.views.vss. The way back from BUG-09.
 *
 * v1 shipped `vssadmin delete shadows /all` inside a checkbox on the Deep preset, and it
 * took every restore point on this machine with it -- then Windows shrank the store to
 * 2.00 GB and left it there (docs/01-AUDIT.md BUG-09, and the memory note recording what
 * this box looks like afterwards). docs/02-SPEC.md 5 answers that with one sentence: the
 * shadow-copy manager "là màn hình riêng, không phải checkbox". This is that screen.
 *
 * Which makes its shape unusual for this app, and deliberately so:
 *
 *   - It is not in the sidebar. app.js reaches it through EXTRA, from the Clean view's
 *     `vss_manage` row, because a permanent nav item for the most destructive screen in
 *     the app would advertise it to every user who never needs it.
 *   - It has no checkbox and no preset. Nothing here can be selected into a batch, so
 *     nothing here can travel with a clean the user approved for something else.
 *   - The two directions are separated on screen and worded oppositely. Raising the
 *     ceiling is the repair and takes one confirmation; deleting copies is the damage and
 *     takes a typed volume letter (docs/02-SPEC.md 4.3). Putting the four buttons in one
 *     row would make them look like four settings of equal weight.
 *
 * Every mutation goes preview -> confirm -> apply, and the preview is what mints the
 * receipt the apply redeems: `pending.token` is the only thing sent back, so nothing this
 * page does between the dialog and the click can widen what runs. The command shown in
 * the dialog is `pending.preview.command`, the argv the engine has held since the dry
 * run -- so what the user reads is what executes, not a re-derivation of it.
 *
 * What this view deliberately does NOT do:
 *
 *   - poll. Nothing here changes on its own; the figures are re-read on enter(), on the
 *     refresh button, and after a mutation, and that is the whole of its liveness.
 *   - offer a custom byte ceiling. The engine accepts one above vssadmin's 320 MB floor,
 *     but the acceptance row (docs/03-PLAN.md:212) is 10 % and UNBOUNDED, and a free
 *     number field here would be a third way to set a system-wide limit for no gain.
 *   - claim a copy count it did not read. `copies_at_risk` comes from the live reading in
 *     the preview, and the dialog quotes it rather than counting rows itself.
 *   - add a second relaunch button. app.js owns the admin banner; an unelevated user is
 *     pointed at the one that is already on screen.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;
  var api = ADC.api;
  /* ADC.app is read at call time, not captured: app.js is the last script in the page,
     so the hub does not exist yet while this file is executing. */

  /* The two limits this screen offers, in the spelling Limit.parse accepts (vss.py:126).
     The engine also ships them in status.suggested; these are here because the buttons
     are labelled before the first read lands. */
  var PERCENT_10 = '10%';
  var UNBOUNDED = 'unbounded';

  /* The glyph i18n prints for a number it cannot format (i18n.js:126), reused for a cell
     with nothing in it so "not read" and "no number" look the same. */
  var DASH = '—';

  var dom = null;
  var state = {
    status: null,      // the last vss_status payload
    error: null,       // an ApiError from the read itself, which is a different failure
    loading: false,
    busy: false        // a preview, its dialog, or an apply is in flight
  };
  /* --- small helpers ---------------------------------------------------------- */

  function bytesText(n) {
    return typeof n === 'number' ? i18n.fmtBytes(n) : DASH;
  }

  /* The size of a volume, from the same fixed_volumes() read the Overview shows. Zero
     when this machine has no such volume, which is what stops a percentage being turned
     into a byte figure nobody measured. */
  function totalOf(letter) {
    var totals = state.status ? state.status.totals : null;
    var key = String(letter || '').toUpperCase();
    return totals && typeof totals[key] === 'number' ? totals[key] : 0;
  }

  /* A limit as the dialog and the toast should read it. `arg` is vssadmin's own spelling
     for the first two kinds -- "10%" needs no translation and UNBOUNDED is a keyword, so
     only the word for "no ceiling" comes from the dictionary. */
  function limitText(limit) {
    if (!limit) { return DASH; }
    if (limit.kind === 'unbounded') { return i18n.t('vss.no_cap'); }
    if (limit.kind === 'bytes') { return i18n.fmtBytes(limit.value); }
    return String(limit.arg || DASH);
  }

  function th(key) {
    return ui.el('th', { class: 'vss__th', i18n: key, attrs: { scope: 'col' } });
  }

  /* Every action button, so one flag can disable the lot while a mutation is in flight.
     Rebuilt by render(), because the buttons are. */
  function setBusy(on) {
    state.busy = on === true;
    if (!dom) { return; }
    for (var i = 0; i < dom.acts.length; i += 1) { dom.acts[i].disabled = state.busy; }
  }
  /* --- the read ---------------------------------------------------------------- */

  /*
   * Re-read the shadow stores. Changes nothing, so it needs no confirmation and no job
   * token: vss_status is two `vssadmin list` commands (docs/02-SPEC.md 5).
   *
   * The previous reading stays on screen while a re-read is in flight. Blanking it would
   * make the refresh button look like it cleared the store.
   */
  function load() {
    if (state.loading) { return Promise.resolve(null); }
    state.loading = true;
    if (!state.status && !state.error) { render(); }
    return api.vssStatus().then(function (data) {
      state.status = data;
      state.error = null;
    }, function (err) {
      /* vss_status answers ok even when it could not read -- `supported` and `error` are
         what the screen draws instead of a table. So a rejection here is the bridge
         itself failing, which is a different sentence from "no shadow copies". */
      state.status = null;
      state.error = err;
    }).then(function () {
      state.loading = false;
      render();
      return null;
    });
  }
  /* --- the refusals ------------------------------------------------------------- */

  /*
   * Four things this screen can be instead of a table, and each says something
   * different. "No data" would say none of them.
   */
  function refusal() {
    if (state.error) {
      return ui.emptyState({
        icon: 'icon-caution', i18n: 'vss.read_failed',
        text: state.error.text ? state.error.text() : String(state.error)
      });
    }
    var s = state.status;
    if (!s) {
      return ui.emptyState({ icon: 'icon-refresh', i18n: 'vss.loading' });
    }
    if (!s.supported) {
      /* s.error is the engine's own sentence about why: no vssadmin, or the service
         refused. Shown verbatim rather than folded into a generic line. */
      return ui.emptyState({
        icon: 'icon-info', i18n: 'vss.unsupported', body: 'vss.unsupported.body',
        text: s.error ? String(s.error) : null
      });
    }
    if (!s.is_admin) {
      return ui.emptyState({
        icon: 'icon-lock', i18n: 'vss.need_admin', body: 'vss.need_admin.body'
      });
    }
    return null;
  }
  /* --- one volume's store -------------------------------------------------------- */

  /* An action button, remembered so setBusy() can reach it. */
  function act(key, iconName, variant, fn) {
    var node = ui.btn({ i18n: key, icon: iconName, variant: variant, on: { click: fn } });
    dom.acts.push(node);
    return node;
  }

  /*
   * One group of buttons under its own heading. Two of these per volume, and the split is
   * the point: the repair and the damage do not belong in one row of four.
   */
  function group(opts) {
    var classes = opts.danger ? 'vss__group vss__group--danger' : 'vss__group';
    return ui.el('div', { class: classes }, [
      ui.el('h3', { class: 'vss__grouptitle' }, [
        ui.icon(opts.danger ? 'icon-dangerous' : 'icon-shield', 'vss__groupico'),
        ui.el('span', { i18n: opts.i18n })
      ]),
      ui.el('p', { class: 'vss__groupsub', i18n: opts.sub }),
      ui.el('div', { class: 'vss__buttons' }, opts.buttons)
    ]);
  }

  /*
   * What the current ceiling works out to as a share of the volume. This is the figure
   * that makes BUG-09 visible: 2.00 GB is not obviously wrong until it is read as 1.5 %
   * of a 134 GB disk. Absent when either number is missing, rather than guessed.
   */
  function capHint(st, total) {
    if (st.unbounded) { return i18n.t('vss.cap_none'); }
    if (typeof st.maximum !== 'number' || total <= 0) { return null; }
    return i18n.t('vss.cap_pct', { pct: i18n.fmtPct(st.maximum / total * 100) });
  }

  function storeCard(st) {
    var letter = st.letter || '';
    var total = totalOf(letter);
    var copies = countCopies(letter);
    var card = ui.card({
      icon: 'icon-disk',
      title: i18n.t('vss.store', { volume: st.volume || letter }),
      /* Which volume the copies actually live on is what `/on=` names, and it is not
         always the same volume -- so it is said on every card, not only when it differs.
         The argv in the dialog carries this letter, and a user comparing the two should
         find the same answer in both places. */
      subText: i18n.t('vss.store.sub', {
        volume: st.volume || letter, on: st.diff_volume || st.volume || letter
      })
    });
    card.body.appendChild(ui.el('div', { class: 'vss__stats' }, [
      ui.stat({ i18n: 'vss.used', value: bytesText(st.used) }),
      ui.stat({ i18n: 'vss.allocated', value: bytesText(st.allocated) }),
      /* The ceiling, and "no ceiling" is a different fact from "0 bytes" -- keeping the
         two apart is most of why this screen exists (vss.py Storage). */
      ui.stat({
        i18n: 'vss.maximum',
        value: st.unbounded ? i18n.t('vss.no_cap') : bytesText(st.maximum),
        hint: capHint(st, total)
      })
    ]));

    var notes = [ui.el('p', { class: 'vss__note', i18n: 'vss.approx' })];
    if (total > 0) {
      /* What the 10 % button would set, in bytes, before it is pressed. Without this the
         choice is between a number and a percentage of an unstated whole. */
      notes.push(ui.el('p', { class: 'vss__note', text: i18n.t('vss.pct_means', {
        pct: PERCENT_10, volume: st.volume || letter,
        size: i18n.fmtBytes(Math.floor(total / 10))
      }) }));
    }
    notes.push(ui.el('p', { class: 'vss__note', text: i18n.tn('vss.copies_here', copies) }));
    card.body.appendChild(ui.el('div', { class: 'vss__notes' }, notes));

    card.body.appendChild(ui.el('div', { class: 'vss__acts' }, [
      group({
        i18n: 'vss.raise', sub: 'vss.raise.sub', buttons: [
          act('vss.set_10', 'icon-shield', 'primary', function () {
            offer(function () { return api.vssResizePreview(letter, PERCENT_10); });
          }),
          act('vss.set_max', 'icon-shield', 'ghost', function () {
            offer(function () { return api.vssResizePreview(letter, UNBOUNDED); });
          })
        ]
      }),
      group({
        i18n: 'vss.drop', sub: 'vss.drop.sub', danger: true, buttons: [
          act('vss.del_oldest', 'icon-dangerous', 'danger', function () {
            offer(function () { return api.vssDeletePreview(letter, 'oldest'); });
          }),
          /* The command v1 ran. It is here because a user who has to free the store
             needs it, and it is the last button on the screen, in the danger group,
             behind a typed volume letter. */
          act('vss.del_all', 'icon-dangerous', 'danger', function () {
            offer(function () { return api.vssDeletePreview(letter, 'all'); });
          })
        ]
      })
    ]));
    return card;
  }

  /* --- the copies ---------------------------------------------------------------- */

  function countCopies(letter) {
    var copies = state.status ? (state.status.copies || []) : [];
    var wanted = String(letter || '').toUpperCase();
    var n = 0;
    for (var i = 0; i < copies.length; i += 1) {
      if (String(copies[i].letter || '').toUpperCase() === wanted) { n += 1; }
    }
    return n;
  }

  /*
   * Every copy on the machine, in one table under the per-volume cards. One table rather
   * than one per volume: a restore point is a date, and the interesting question about
   * dates is which is oldest -- which is the copy `/oldest` takes.
   */
  function copiesCard() {
    var copies = state.status ? (state.status.copies || []) : [];
    var card = ui.card({ i18n: 'vss.copies', sub: 'vss.copies.sub', icon: 'icon-history' });
    if (!copies.length) {
      card.body.appendChild(ui.emptyState({
        icon: 'icon-info', i18n: 'vss.copies.none', body: 'vss.copies.none.body'
      }));
      return card;
    }
    var tbody = ui.el('tbody', null, copies.map(function (copy) {
      return ui.el('tr', null, [
        ui.el('td', { class: 'vss__td', text: copy.letter ? copy.letter + ':' : DASH }),
        /* `created` is the string the service printed, not a parsed date: reformatting it
           would mean parsing a localised timestamp, and a restore point shown under the
           wrong day is worse than one shown in Windows' own wording (vss.py ShadowCopy). */
        ui.el('td', { class: 'vss__td mono', text: copy.created || DASH }),
        ui.el('td', { class: 'vss__td', text: copy.provider || DASH }),
        ui.el('td', { class: 'vss__td mono', text: copy.copy_id || DASH })
      ]);
    }));
    card.body.appendChild(ui.el('div', { class: 'vss__table-wrap' }, [
      ui.el('table', { class: 'vss__table' }, [
        ui.el('caption', { class: 'vss__caption', i18n: 'vss.copies.caption' }),
        ui.el('thead', null, [ui.el('tr', null, [
          th('vss.col.volume'), th('vss.col.created'),
          th('vss.col.provider'), th('vss.col.id')
        ])]),
        tbody
      ])
    ]));
    return card;
  }
  /* --- the mutations ------------------------------------------------------------- */

  /*
   * The figures in the confirmation dialog. Every one is quoted out of `pending` -- the
   * payload the preview returned -- so the dialog cannot describe an action that the
   * token it is about to spend does not name.
   */
  function dialogLines(p) {
    var out = [i18n.t('vss.line.volume', { volume: p.volume || p.letter || DASH })];
    if (p.action === 'resize') {
      out.push(i18n.t('vss.line.limit', { limit: limitText(p.limit) }));
    } else {
      out.push(i18n.t(p.scope === 'all' ? 'vss.line.all' : 'vss.line.oldest'));
      /* The engine's own count, from the live reading it took during the preview. */
      out.push(i18n.tn('vss.line.at_risk', p.copies_at_risk || 0));
    }
    out.push(i18n.t('vss.line.on', { volume: p.on_volume || p.volume || DASH }));
    /* The argv the engine has held since the dry run, verbatim: what the user reads here
       is what runs, not a re-derivation of it. */
    out.push(i18n.t('vss.line.command', {
      command: (p.preview && p.preview.command) || DASH
    }));
    /* The receipt's real expiry, not a sentence about the TTL -- the engine ships
       `expires_at` and a hard-coded "5 minutes" would drift from it. */
    out.push(i18n.t('vss.line.expires', { clock: i18n.fmtClock(p.expires_at) }));
    return out;
  }
  /*
   * preview -> confirm -> apply, for all four buttons. `start` is the preview call; every
   * word of the dialog after it comes out of what that preview returned.
   *
   * The token is the whole of what travels back. A destructive action also sends the typed
   * text, which the engine checks against its own record BEFORE it spends the token
   * (bridge.py _vss_phrase) -- the dialog's own check is a courtesy, not the gate.
   *
   * Note what is keyed on what: the wording follows `action`, because a shrink is still a
   * resize and calling it a delete would be a lie; the danger styling, the note and the
   * typed phrase follow `destructive`, which plan_resize sets for a shrink and for a
   * percentage it could not measure (vss.py, fails closed).
   */
  function offer(start) {
    if (state.busy) { return; }
    setBusy(true);
    var pending = null;
    start().then(function (p) {
      pending = p;
      var resize = p.action === 'resize';
      return ui.confirm({
        i18n: resize ? 'vss.ask.resize' : 'vss.ask.delete',
        body: resize ? 'vss.ask.resize.body' : 'vss.ask.delete.body',
        confirmKey: resize ? 'vss.ask.resize_go' : 'vss.ask.delete_go',
        danger: p.destructive === true,
        lines: dialogLines(p),
        note: p.destructive ? i18n.t('vss.ask.note') : null,
        phrase: p.destructive ? p.phrase : null
      });
    }).then(function (answer) {
      if (!answer) { setBusy(false); return null; }
      /* `answer` is the typed text on the destructive path and `true` on the other, and
         vss_apply takes null for "no phrase was asked for". */
      return api.vssApply(pending.token, typeof answer === 'string' ? answer : null)
        .then(applied);
    }).then(null, function (err) {
      setBusy(false);
      ui.showError(err);
    });
  }
  /*
   * The far end of the handshake. `reply` is {action, result}: the receipt as the engine
   * recorded it, and what the command actually did.
   *
   * A non-zero exit is not an exception -- vssadmin ran and refused, and the engine has a
   * sentence about why -- so it is a toast rather than showError(), which is for a bridge
   * that could not be reached at all.
   */
  function applied(reply) {
    var a = (reply && reply.action) || {};
    var r = (reply && reply.result) || {};
    setBusy(false);
    if (r.ok) {
      ui.toast(a.action === 'resize'
        ? i18n.t('vss.done.resize', {
          volume: a.volume || DASH, limit: limitText(a.limit)
        })
        : i18n.t('vss.done.delete', { volume: a.volume || DASH }),
      { kind: 'success', detail: r.command });
    } else {
      /* r.reason is the engine's sentence, r.lines what the command printed. The reason
         goes in the toast's detail line so the body can stay one sentence long. */
      ui.toast(i18n.t('vss.failed'), { kind: 'error', detail: r.reason || r.command });
    }
    /* Re-read either way. A resize that failed can still have moved the store, and a user
       left looking at the old figures has no way to tell. */
    return load();
  }
  /* --- render --------------------------------------------------------------------- */

  /*
   * One card per storage, then the copies table. Rebuilt whole rather than patched: this
   * screen is four numbers and a short table, and a diff would be more code than the
   * redraw it saves.
   */
  function render() {
    if (!dom) { return; }
    ui.clear(dom.body);
    /* The buttons are about to be thrown away, so the list that tracks them starts over. */
    dom.acts = [];
    var no = refusal();
    if (no) {
      dom.body.appendChild(no);
      return;
    }
    var storages = state.status.storages || [];
    if (!storages.length) {
      /* vssadmin answered and listed nothing: shadow copies are off on every volume, which
         is a state the user can act on and not an error. */
      dom.body.appendChild(ui.emptyState({
        icon: 'icon-info', i18n: 'vss.no_store', body: 'vss.no_store.body'
      }));
    } else {
      for (var i = 0; i < storages.length; i += 1) {
        dom.body.appendChild(storeCard(storages[i]));
      }
    }
    dom.body.appendChild(copiesCard());
    /* The flag outlives the buttons it disabled, so it is re-applied to the new ones --
       otherwise a redraw during an apply would hand back four live buttons. */
    setBusy(state.busy);
  }
  /* --- lifecycle ------------------------------------------------------------------ */

  function mount(host) {
    var body = ui.el('div', { class: 'vss__body' });
    /* What the screen is for, said once at the top. A user arrives here from a row in the
       Clean view, so the first thing on the page has to explain where they landed. */
    var lede = ui.card({ i18n: 'vss.about', sub: 'vss.about.sub', icon: 'icon-shield' });
    lede.body.appendChild(ui.el('p', { class: 'vss__lede', i18n: 'vss.about.body' }));
    host.appendChild(ui.el('div', { class: 'vss' }, [lede, body]));
    dom = { body: body, acts: [] };
    render();
  }

  function enter(host, actions) {
    /* The way out. This view has no nav item by design, so without this the only exit is a
       sidebar click -- and the row that opens it lives on Clean. */
    actions.appendChild(ui.btn({
      i18n: 'vss.back', icon: 'icon-chevron', variant: 'ghost',
      on: { click: function () { ADC.app.go('clean'); } }
    }));
    actions.appendChild(ui.btn({
      i18n: 'vss.reload', icon: 'icon-refresh', variant: 'ghost',
      on: { click: function () { load(); } }
    }));
    load();
  }

  /* Nothing to stop: no job, no timer, and a mutation in flight is a bridge call that will
     finish whether or not this view is on screen. The function exists to say so. */
  function leave() {
    return null;
  }

  /* i18n.apply() has already retranslated every data-i18n node in the document. What it
     cannot reach is the strings this file formatted itself -- the byte figures, the
     percentages, the two volume sentences -- and those come back with a redraw of data
     already in hand. No bridge call: the engine's numbers did not change, only the
     language they are read in. */
  function relang() {
    render();
  }

  var views = ADC.views = ADC.views || {};
  views.vss = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
