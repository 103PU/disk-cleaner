/*
 * views/settings.js -- ADC.views.settings. Every engine setting, in one page.
 *
 * The engine owns the settings; this view is a window onto them and keeps nothing of its
 * own. That is the whole design, and it comes from one failure mode: a preferences pane
 * that holds a private copy of the values will quietly disagree with the profile on disk
 * the first time a write is clamped or refused. So:
 *
 *   - every write goes through ADC.app.saveSettings, which is also what tells the four
 *     other views that read these values (app.js:135-143). No settings_set call is here;
 *   - every render is built from the settings object the bridge ANSWERED WITH, never from
 *     the changes that were sent. settings_set clamps a number, drops a relative or
 *     wildcarded exclusion, and may coerce an unknown preset -- all silently -- so "what
 *     was asked for" and "what is in force" are two objects and only the second is true;
 *   - the only things this module caches are two lists that are not settings at all: the
 *     preset names off the catalogue, and the volume rows. The page cannot invent either.
 *
 * A number the engine would clamp is clamped here first, out loud. A user who types 99999
 * hours and is then shown 99999 while 8760 sits on disk has been told something false.
 *
 * What this view deliberately does NOT do: disable anything while a job runs (a plan
 * snapshots the settings when it is built, so a mid-scan change cannot alter what was
 * already approved, and grey-out would only look broken); start, watch or cancel a job;
 * poll; or add a second "open log file" button, which the console pane already owns.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;
  var i18n = ADC.i18n;

  /* ADC.app is deliberately not captured: app.js is the last script tag in the document
     (index.html:308) and every view file is evaluated before it, so the object does not
     exist at this line. ui, i18n and ADC.api load first and are safe to hold. */

  /* The caps settings_set enforces, restated so a clamp can be announced BEFORE the value
     leaves the page. If they drift, the engine wins and the re-render shows the real one. */
  var MAX_MIN_AGE_HOURS = 8760;
  var MAX_SCAN_BUDGET_S = 3600;
  var MAX_EXCLUSIONS = 64;

  /* Not a catalogue preset: the page's own way of saying "leave my selection alone". */
  var CUSTOM = 'custom';
  var SAVED_FLASH_MS = 2400;

  var host = null;          /* the <section> app.js handed to mount */
  var body = null;          /* the part render() throws away and rebuilds */
  var statusLine = null;    /* outside body, so a rebuild does not wipe the saved flash */
  var active = false;       /* true between enter() and leave() */
  var saving = false;       /* re-entry guard against our own onSettings notification */
  var presetNames = null;   /* Object.keys(catalog.presets); null until it lands */
  var volumeRows = null;    /* volumes().volumes; null until they land */
  var volumesFailed = false;
  var savedTimer = null;

  /* --- control builders --------------------------------------------------------- */

  /*
   * ui.field plus the one thing it does not do: name the help paragraph and point the
   * control at it with aria-describedby, so "0 means no budget" is announced WITH the input
   * rather than only when a screen reader user tabs past it. ui.field already sets
   * control.id and the label's for.
   */
  function mkField(o) {
    var node = ui.field(o);
    if (o.help && o.id && o.control) {
      var help = node.querySelector('.field__help');
      if (help) {
        help.id = o.id + '-help';
        o.control.setAttribute('aria-describedby', help.id);
      }
    }
    return node;
  }

  /* One card per group. The subtitle key is the title key plus ".sub" by convention, so a
     card cannot half-exist in the dictionary, and the fields go in one wrapper layout.css
     can space as a unit. */
  function sectionCard(titleKey, iconName, fields) {
    var c = ui.card({
      i18n: titleKey, sub: titleKey + '.sub', icon: iconName, class: 'settings__card'
    });
    c.body.appendChild(ui.el('div', { class: 'settings__fields' }, fields));
    return c;
  }

  /*
   * A select whose option labels are dictionary keys -- except where the value is an engine
   * identifier this build has no label for, which goes in as free text. A select that cannot
   * represent the stored value shows some other row as selected, and the next change writes
   * back something the user never picked.
   */
  function select(value, options, onPick) {
    var node = ui.el('select', { class: 'settings__select', on: { change: onPick } });
    for (var i = 0; i < options.length; i += 1) {
      var o = options[i];
      node.appendChild(o.i18n
        ? ui.el('option', { value: o.value, i18n: o.i18n })
        : ui.el('option', { value: o.value, text: o.text }));
    }
    node.value = (value === null || value === undefined) ? '' : String(value);
    return node;
  }

  function checkbox(checked, onToggle) {
    /* type first, and the order matters: ui.el assigns in key order, and checked set while
       the element is still a text input does not survive the switch. */
    return ui.el('input', {
      type: 'checkbox', class: 'settings__check',
      checked: checked === true,
      on: { change: onToggle }
    });
  }

  function numberInput(value, max, onCommit) {
    return ui.el('input', {
      type: 'number', class: 'settings__num num',
      min: '0', max: String(max), step: '1', inputmode: 'numeric',
      value: typeof value === 'number' ? String(value) : '',
      /* change, not input: on input a user typing 1234 would write 1, then 12, then 123 to
         the profile and fire four notifications on the way. */
      on: { change: onCommit }
    });
  }

  /*
   * A committed number, clamped to the engine's cap and SAID OUT LOUD when the clamp bit.
   * min= and max= are validation hints and clamp nothing; settings_set clamps in silence.
   *
   * Returns null when there is no number to save -- an empty box, or characters a number
   * input refuses, which Chromium reports as '' rather than as what was typed. The caller
   * then restores what the engine holds; it does not invent a value.
   */
  function readNumber(input, max, clampKey) {
    var raw = input.value;
    if (raw === null || raw === undefined || String(raw) === '') { return null; }
    var n = Number(raw);
    if (!isFinite(n)) { return null; }
    n = Math.floor(n);
    if (n < 0) {
      n = 0;
      ui.toast(i18n.t('settings.clamped_zero'), { kind: 'warn' });
    } else if (n > max) {
      n = max;
      ui.toast(i18n.t(clampKey, { max: i18n.fmtInt(max) }), { kind: 'warn' });
    }
    /* Show the clamped figure before it is sent, so the control and the toast agree. */
    input.value = String(n);
    return n;
  }

  /* One numeric field off the hub, for the revert paths: a control that rejected what was
     typed goes back to what the engine holds, not to what this view rendered earlier. */
  function liveNum(key) {
    var s = ADC.app.settings();
    var v = s ? s[key] : null;
    return typeof v === 'number' ? String(v) : '';
  }

  /* --- the write path ----------------------------------------------------------- */

  /*
   * The settings object to render from, whatever shape the answer arrived in.
   * ADC.app.saveSettings resolves with the bare settings object (app.js:135-143),
   * settings_set itself answers {settings, persisted}, and the hub is the fallback for
   * both. What this never reads is the changes that were sent.
   */
  function pickSettings(res) {
    if (res && typeof res === 'object') {
      if (res.settings && typeof res.settings === 'object') { return res.settings; }
      if (typeof res.schema_version !== 'undefined') { return res; }
    }
    return ADC.app.settings();
  }

  /*
   * The only write in this file.
   *
   * saving is held for the whole round trip because saveSettings notifies every listener
   * (app.js:146-160) including this view's own, and re-rendering from inside our own
   * notification would tear down the control the user is still holding, then do it again
   * when the promise settles. No claimJob: nothing here starts a job.
   */
  function save(changes, done) {
    saving = true;
    return ADC.app.saveSettings(changes).then(function (res) {
      saving = false;
      var next = pickSettings(res);
      if (active) { render(next); flashSaved(); }
      if (done) { done(next); }
      return next;
    }, function (err) {
      saving = false;
      ui.showError(err);
      /* A refused save must not leave a control showing a value nothing agreed to. */
      if (active) { render(); }
      return null;
    });
  }

  /*
   * "Saved." in a polite live region. Every other signal this page gives is negative -- a
   * clamp toast, a dropped-path toast, app.js's settings.not_persisted warning -- and a
   * checkbox that snaps back to the same position reads as "nothing happened".
   *
   * The key goes on as data-i18n as well as text, so app.js's i18n.apply(document) relabels
   * the line on a language switch without this view remembering what it said.
   */
  function flashSaved() {
    if (!statusLine) { return; }
    statusLine.setAttribute('data-i18n', 'settings.saved');
    statusLine.textContent = i18n.t('settings.saved');
    if (savedTimer !== null) { window.clearTimeout(savedTimer); }
    savedTimer = window.setTimeout(clearStatus, SAVED_FLASH_MS);
  }

  function clearStatus() {
    if (savedTimer !== null) { window.clearTimeout(savedTimer); savedTimer = null; }
    if (!statusLine) { return; }
    statusLine.removeAttribute('data-i18n');
    statusLine.textContent = '';
  }

  /*
   * The exclusion list, and the only place a user can find out what the engine did with it.
   * settings_set drops a relative path, a wildcarded one, an over-long one and everything
   * past the 64th entry, without a word. So the textarea is rebuilt from the RETURNED list
   * after every save, and a shorter list is toasted -- otherwise a dropped line looks
   * exactly like an accepted one.
   */
  function saveExclusions(area) {
    var lines = String(area.value).split(/\r?\n/);
    var sent = [];
    for (var i = 0; i < lines.length; i += 1) {
      var line = lines[i].trim();
      if (line !== '') { sent.push(line); }
    }
    save({ exclusions: sent }, function (next) {
      var kept = (next && next.exclusions) ? next.exclusions.length : 0;
      var dropped = sent.length - kept;
      if (dropped > 0) {
        ui.toast(i18n.tn('settings.exclusions.dropped', dropped), { kind: 'warn' });
      }
    });
  }

  /* Back to the engine's defaults, behind a danger confirm whose body says no files are
     deleted -- a red button on a disk cleaner is assumed to remove something. */
  function resetToDefaults() {
    var defaults = ADC.app.defaults();
    if (!defaults) {
      ui.toast(i18n.t('settings.reset.unavailable'), { kind: 'warn' });
      return;
    }
    ui.confirm({
      i18n: 'settings.reset.confirm',
      body: 'settings.reset.confirm.body',
      confirmKey: 'settings.reset.confirm.ok',
      danger: true
    }).then(function (ok) {
      if (!ok) { return; }
      /* Every key of defaults(), in ONE call: field by field would fire nine notifications
         and could stop halfway, leaving a profile that is neither the old settings nor the
         defaults. settings_set coerces its own input, so a key it does not own is its
         business to ignore rather than ours to filter here. */
      var changes = {};
      var keys = Object.keys(defaults);
      for (var i = 0; i < keys.length; i += 1) { changes[keys[i]] = defaults[keys[i]]; }
      save(changes, function (next) {
        ui.toast(i18n.t('settings.reset.done'), { kind: 'success' });
        /* A reset can change the language, and then the page follows the profile rather than
           the other way round. app.js hears i18n.onChange and calls relang(). */
        if (next && next.language && next.language !== i18n.lang) {
          i18n.setLang(next.language);
        }
      });
    });
  }

  /* --- the three cards ---------------------------------------------------------- */

  /*
   * The preset list comes from the catalogue, not from a constant here: the engine owns
   * which presets exist, and hardcoding "safe" and "deep" is how a third one would ship
   * invisible. "custom" is appended because it is not a catalogue entry at all.
   */
  function presetOptions(current) {
    var opts = [];
    var names = presetNames || [];
    var seen = {};
    for (var i = 0; i < names.length; i += 1) {
      seen[names[i]] = true;
      opts.push({ value: names[i], i18n: 'settings.preset.' + names[i] });
    }
    if (!seen[CUSTOM]) { opts.push({ value: CUSTOM, i18n: 'settings.preset.custom' }); }
    /* Whatever is stored must be representable -- before the catalogue lands, or if the
       engine grows a preset this build has no label for. Free text: an engine identifier is
       not a translatable string. */
    if (current && current !== CUSTOM && !seen[current]) {
      opts.unshift({ value: current, text: String(current) });
    }
    return opts;
  }

  function languageCard(s) {
    var langId = 'set-language';
    var presetId = 'set-preset';

    /* The select shows i18n.lang, not s.language. The two differ for exactly as long as a
       switch is in flight, and in that window the live language is what the user is looking
       at; reading s.language would snap the control back on the relang() re-render and then
       forward again when the save landed. */
    var langSel = select(i18n.lang, [
      { value: 'vi', i18n: 'lang.vi' },
      { value: 'en', i18n: 'lang.en' }
    ], function (evt) {
      var value = evt.target.value;
      /* Switch the screen FIRST, then write it down: the language is what the user asked
         for and the disk write is a consequence. app.js hears i18n.onChange and calls
         relang(), so nothing here re-applies anything itself. */
      i18n.setLang(value);
      save({ language: value });
    });

    var presetSel = select(s.preset, presetOptions(s.preset), function (evt) {
      save({ preset: evt.target.value });
    });

    return sectionCard('settings.card.language', 'icon-settings', [
      mkField({ i18n: 'lang.label', id: langId, control: langSel,
        help: 'settings.language.help' }),
      mkField({ i18n: 'settings.preset', id: presetId, control: presetSel,
        help: 'settings.preset.help' })
    ]);
  }

  function scanningCard(s) {
    var sodId = 'set-size-on-disk';
    var ageId = 'set-min-age';
    var budgetId = 'set-scan-budget';
    var volId = 'set-volumes';

    var sod = checkbox(s.size_on_disk === true, function (evt) {
      save({ size_on_disk: evt.target.checked === true });
    });
    var age = numberInput(s.min_age_hours, MAX_MIN_AGE_HOURS, function (evt) {
      var n = readNumber(evt.target, MAX_MIN_AGE_HOURS, 'settings.min_age.clamped');
      if (n === null) { evt.target.value = liveNum('min_age_hours'); return; }
      save({ min_age_hours: n });
    });
    var budget = numberInput(s.scan_budget_s, MAX_SCAN_BUDGET_S, function (evt) {
      var n = readNumber(evt.target, MAX_SCAN_BUDGET_S, 'settings.budget.clamped');
      if (n === null) { evt.target.value = liveNum('scan_budget_s'); return; }
      save({ scan_budget_s: n });
    });

    return sectionCard('settings.card.scanning', 'icon-disk', [
      mkField({ i18n: 'settings.size_on_disk', id: sodId, control: sod,
        help: 'settings.size_on_disk.help' }),
      mkField({ i18n: 'settings.min_age', id: ageId, control: age,
        help: 'settings.min_age.help' }),
      mkField({ i18n: 'settings.budget', id: budgetId, control: budget,
        help: 'settings.budget.help' }),
      /* volume_ids sits with the scanning fields rather than in field order: it scopes the
         walk, and the safety card is about what may be deleted. */
      mkField({ i18n: 'settings.volumes', id: volId, control: volumeGroup(s, volId),
        help: 'settings.volumes.help' })
    ]);
  }

  function safetyCard(s) {
    var confirmId = 'set-confirm-dangerous';
    var restoreId = 'set-restore-point';
    var excId = 'set-exclusions';
    var resetId = 'set-reset';

    var confirmBox = checkbox(s.confirm_dangerous === true, function (evt) {
      save({ confirm_dangerous: evt.target.checked === true });
    });
    var restoreBox = checkbox(s.restore_point_before_dangerous === true, function (evt) {
      save({ restore_point_before_dangerous: evt.target.checked === true });
    });

    var stored = s.exclusions || [];
    var area = ui.el('textarea', {
      class: 'settings__area mono', rows: 6, spellcheck: false,
      /* One absolute path per line. Every line is arbitrary text off a filesystem and
         reaches the DOM as a property value, never as markup. */
      value: stored.join('\n')
    });

    /* The save button sits OUTSIDE ui.field on purpose: field() ties its label to one control
       with the for attribute, and a label pointing at a wrapper holding both a textarea and a
       button points at nothing useful. The textarea is the field's control; button and count
       go underneath. */
    var excActions = ui.el('div', { class: 'settings__actions' }, [
      ui.btn({
        i18n: 'settings.exclusions.save', icon: 'icon-shield', variant: 'primary',
        on: { click: function () { saveExclusions(area); } }
      }),
      /* How close the list is to the cap, because the 65th entry is dropped in silence and
         counting lines by hand is the alternative. */
      ui.el('p', {
        class: 'settings__hint',
        text: i18n.tn('settings.exclusions.count', stored.length,
          { max: i18n.fmtInt(MAX_EXCLUSIONS) })
      })
    ]);

    return sectionCard('settings.card.safety', 'icon-shield', [
      mkField({ i18n: 'settings.confirm_dangerous', id: confirmId, control: confirmBox,
        help: 'settings.confirm_dangerous.help' }),
      mkField({ i18n: 'settings.restore_point', id: restoreId, control: restoreBox,
        help: 'settings.restore_point.help' }),
      mkField({ i18n: 'settings.exclusions', id: excId, control: area,
        help: 'settings.exclusions.help' }),
      excActions,
      mkField({ i18n: 'settings.reset', id: resetId, help: 'settings.reset.help',
        control: ui.btn({
          i18n: 'settings.reset.button', icon: 'icon-refresh', variant: 'danger',
          on: { click: resetToDefaults }
        }) })
    ]);
  }

  /*
   * One checkbox per lettered volume; ticking none means every volume, which the help text
   * states rather than leaving as folklore.
   *
   * A volume with no drive letter -- a mounted folder, an unlettered recovery partition -- is
   * skipped: volume_ids holds letters and nothing else, so a checkbox for one could be ticked
   * but never saved, and an unsaveable control is worse than an absent one.
   */
  function volumeGroup(s, groupId) {
    var group = ui.el('div', {
      class: 'settings__vols',
      role: 'group',
      /* ui.field draws the visible label, but a role="group" needs its own accessible name
         or a screen reader announces several loose checkboxes. */
      i18nAttr: 'aria-label:settings.volumes'
    });

    if (volumesFailed) {
      group.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.volumes.failed' }));
      group.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'action.retry', icon: 'icon-refresh', variant: 'ghost', size: 'sm',
          on: { click: function () { loadVolumes(true); } }
        })
      ]));
      return group;
    }
    if (volumeRows === null) {
      group.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.volumes.loading' }));
      return group;
    }

    var chosen = {};
    var stored = s.volume_ids || [];
    for (var c = 0; c < stored.length; c += 1) {
      chosen[String(stored[c]).toUpperCase()] = true;
    }
    var boxes = [];

    /* One handler for the group: the engine takes the whole list, not a delta, so every
       change re-reads every box. */
    function commit() {
      var picked = [];
      for (var b = 0; b < boxes.length; b += 1) {
        if (boxes[b].checked) { picked.push(boxes[b].value); }
      }
      save({ volume_ids: picked });
    }

    for (var i = 0; i < volumeRows.length; i += 1) {
      var vol = volumeRows[i] || {};
      if (!vol.letter) { continue; }
      var letter = String(vol.letter).toUpperCase();
      var boxId = groupId + '-' + letter;
      var box = ui.el('input', {
        type: 'checkbox', id: boxId, class: 'settings__check',
        value: letter, checked: chosen[letter] === true,
        on: { change: commit }
      });
      boxes.push(box);
      group.appendChild(ui.el('label', { class: 'settings__vol', attrs: { for: boxId } }, [
        box,
        /* display_name is free text off the disk -- a label somebody typed in Explorer. */
        ui.el('span', {
          class: 'settings__vol-name',
          text: vol.display_name || vol.root || letter
        })
      ]));
    }
    if (!boxes.length) {
      group.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.volumes.none' }));
    }
    return group;
  }

  /* --- application updates ------------------------------------------------------- */

  var updateState = {
    checked: false,
    checking: false,
    info: null,
    progress: null,
    pollTimer: null,
    error: null
  };

  function stopUpdatePoll() {
    if (updateState.pollTimer) {
      window.clearTimeout(updateState.pollTimer);
      updateState.pollTimer = null;
    }
  }

  function pollUpdateProgress() {
    stopUpdatePoll();
    ADC.api.updaterDownloadProgress().then(function (res) {
      if (!res || !res.progress) { return; }
      updateState.progress = res.progress;
      if (updateState.progress.status === 'downloading') {
        if (active) { render(); }
        updateState.pollTimer = window.setTimeout(pollUpdateProgress, 300);
      } else {
        if (active) { render(); }
      }
    }, function () {
      stopUpdatePoll();
    });
  }

  function checkUpdates(force) {
    updateState.checking = true;
    updateState.error = null;
    if (active) { render(); }
    ADC.api.updaterCheck(force === true).then(function (res) {
      updateState.checking = false;
      updateState.checked = true;
      updateState.info = res ? res.info : null;
      if (active) { render(); }
    }, function (err) {
      updateState.checking = false;
      updateState.checked = true;
      updateState.error = (err && err.text) ? err.text() : String(err && err.message ? err.message : err);
      if (active) { render(); }
    });
  }

  function startUpdateDownload() {
    ADC.api.updaterDownloadStart().then(function (res) {
      if (res && res.progress) {
        updateState.progress = res.progress;
      }
      if (active) { render(); }
      pollUpdateProgress();
    }, function (err) {
      ui.showError(err);
    });
  }

  function cancelUpdateDownload() {
    stopUpdatePoll();
    ADC.api.updaterDownloadCancel().then(function (res) {
      if (res && res.progress) {
        updateState.progress = res.progress;
      }
      if (active) { render(); }
    }, function (err) {
      ui.showError(err);
    });
  }

  function installUpdate() {
    ADC.api.updaterInstall().then(function () {
      ui.toast(i18n.t('settings.updates.installing'), { kind: 'info' });
    }, function (err) {
      ui.showError(err);
    });
  }

  function updatesCard() {
    var c = ui.card({
      i18n: 'settings.updates', sub: 'settings.updates.sub', icon: 'icon-refresh', class: 'settings__card'
    });

    var fields = ui.el('div', { class: 'settings__fields' });
    var infoBox = ui.el('div', { class: 'settings__update-info' });

    var curLine = ui.el('p', {}, [
      ui.el('span', { i18n: 'settings.updates.current' }),
      ui.el('span', { class: 'mono', text: 'v' + '2.0.0' })
    ]);
    infoBox.appendChild(curLine);

    if (updateState.checking) {
      infoBox.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.updates.checking' }));
      fields.appendChild(infoBox);
      c.body.appendChild(fields);
      return c;
    }

    if (updateState.error) {
      infoBox.appendChild(ui.el('p', { class: 'settings__hint' }, [
        ui.el('span', { i18n: 'settings.updates.failed' }),
        document.createTextNode(updateState.error)
      ]));
      fields.appendChild(infoBox);
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.check_btn', icon: 'icon-refresh', variant: 'primary',
          on: { click: function () { checkUpdates(true); } }
        })
      ]));
      c.body.appendChild(fields);
      return c;
    }

    if (!updateState.checked) {
      infoBox.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.updates.idle' }));
      fields.appendChild(infoBox);
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.check_btn', icon: 'icon-refresh', variant: 'primary',
          on: { click: function () { checkUpdates(true); } }
        })
      ]));
      c.body.appendChild(fields);
      return c;
    }

    var info = updateState.info;
    if (!info || !info.available) {
      infoBox.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.updates.latest' }));
      fields.appendChild(infoBox);
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.recheck_btn', icon: 'icon-refresh', variant: 'ghost',
          on: { click: function () { checkUpdates(true); } }
        })
      ]));
      c.body.appendChild(fields);
      return c;
    }

    infoBox.appendChild(ui.el('p', {}, [
      ui.el('strong', { i18n: 'settings.updates.available_title' }),
      ui.el('span', { class: 'mono', text: info.latest_version + ' (' + i18n.fmtBytes(info.asset_size) + ')' })
    ]));

    if (info.release_notes) {
      infoBox.appendChild(ui.el('div', { class: 'settings__update-notes', text: info.release_notes }));
    }

    fields.appendChild(infoBox);

    var prog = updateState.progress;
    if (prog && prog.status === 'downloading') {
      var progContainer = ui.el('div', { class: 'settings__update-prog' });
      var pBar = ui.progress();
      var pct = typeof prog.pct === 'number' ? prog.pct : 0;
      var mbDone = (prog.bytes_downloaded / (1024 * 1024)).toFixed(1);
      var mbTotal = (prog.total_bytes / (1024 * 1024)).toFixed(1);
      pBar.set(pct, pct + '% (' + mbDone + ' MB / ' + mbTotal + ' MB)');
      progContainer.appendChild(pBar);

      fields.appendChild(progContainer);
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.cancel_btn', icon: 'icon-stop', variant: 'ghost',
          on: { click: cancelUpdateDownload }
        })
      ]));
    } else if (prog && prog.status === 'completed') {
      fields.appendChild(ui.el('p', { class: 'settings__hint', i18n: 'settings.updates.completed' }));
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.install_btn', icon: 'icon-play', variant: 'primary',
          on: { click: installUpdate }
        })
      ]));
    } else {
      fields.appendChild(ui.el('div', { class: 'settings__actions' }, [
        ui.btn({
          i18n: 'settings.updates.download_btn', icon: 'icon-play', variant: 'primary',
          on: { click: startUpdateDownload }
        }),
        ui.btn({
          i18n: 'settings.updates.recheck_btn', icon: 'icon-refresh', variant: 'ghost',
          on: { click: function () { checkUpdates(true); } }
        })
      ]));
    }

    c.body.appendChild(fields);
    return c;
  }

  /* --- schema version, and the no-settings panel -------------------------------- */

  /* schema_version, small and dim. An identifier, not a quantity, so it does NOT go through
     fmtInt: a thousands separator in a version number would be wrong in both languages. */
  function versionLine(s) {
    return ui.el('p', { class: 'settings__version' }, [
      ui.el('span', { i18n: 'settings.schema' }),
      ' ',
      ui.el('span', {
        class: 'settings__version-num mono',
        text: (s.schema_version === null || s.schema_version === undefined)
          ? '—' : String(s.schema_version)
      })
    ]);
  }

  function loadFailedPanel() {
    return ui.emptyState({
      icon: 'icon-caution',
      i18n: 'settings.load_failed',
      body: 'settings.load_failed.body',
      action: [ui.btn({
        i18n: 'action.retry', icon: 'icon-refresh', variant: 'primary',
        on: { click: retryLoad }
      })]
    });
  }

  /*
   * The retry behind that panel: ask the engine again, then re-enter.
   *
   * ADC.app owns the settings hub and exposes no way to refill it -- loadSettings is private
   * to app.js (app.js:126-131) and api.settingsGet() does not touch state.settings -- so if
   * the hub is still empty after a successful read, this renders from the payload the bridge
   * just answered with. Nothing is stored: the next write still goes through
   * ADC.app.saveSettings, which is what fills the hub for every other view.
   */
  function retryLoad() {
    ADC.api.settingsGet().then(function (payload) {
      if (!active) { return; }
      enter(host);
      if (!ADC.app.settings()) { render(payload ? payload.settings : null); }
    }, ui.showError);
  }

  /* --- render -------------------------------------------------------------------- */

  /*
   * Rebuild, do not patch. Ten controls and no live data means a rebuild costs nothing, and
   * it is the only way a language switch relabels every option, re-picks the selected one and
   * reformats the exclusion count without a second code path to keep in step.
   *
   * The focused id is read before the clear and restored after, so a re-render provoked by
   * another view's save does not drop a keyboard user at the top of the document.
   */
  function render(given) {
    if (!body) { return; }
    var s = given || ADC.app.settings();
    var refocus = (document.activeElement && body.contains(document.activeElement))
      ? document.activeElement.id : '';
    ui.clear(body);
    if (!s) {
      body.appendChild(loadFailedPanel());
      return;
    }
    body.appendChild(languageCard(s));
    body.appendChild(scanningCard(s));
    body.appendChild(safetyCard(s));
    body.appendChild(updatesCard(s));
    body.appendChild(versionLine(s));
    if (refocus) {
      var again = document.getElementById(refocus);
      if (again && again.focus) { again.focus(); }
    }
  }

  /* --- the two lists this page cannot invent ------------------------------------- */

  function loadCatalog() {
    ADC.app.catalog().then(function (cat) {
      presetNames = (cat && cat.presets) ? Object.keys(cat.presets) : [];
      if (active) { render(); }
    }, function (err) {
      /* An empty list still leaves the stored preset selectable, because presetOptions puts
         it back as free text. */
      presetNames = [];
      if (active) { render(); }
      ui.showError(err);
    });
  }

  function loadVolumes(force) {
    volumesFailed = false;
    ADC.app.volumes(force).then(function (data) {
      volumeRows = (data && data.volumes) ? data.volumes : [];
      if (active) { render(); }
    }, function (err) {
      /* "The volumes could not be read" and "there are no volumes" are different sentences,
         and this page must not print the second when the first is true. */
      volumeRows = [];
      volumesFailed = true;
      if (active) { render(); }
      ui.showError(err);
    });
  }

  /* --- lifecycle ----------------------------------------------------------------- */

  /* Another view changed a setting -- the Clean page's dangerous-confirm toggle is the obvious
     one -- and this page must not sit on a stale value. Our own save is excluded by saving,
     since it re-renders from what the bridge returned a moment later. */
  function onExternalChange(next) {
    if (saving || !active) { return; }
    render(next && typeof next === 'object' ? next : null);
  }

  function mount(hostEl) {
    host = hostEl;
    statusLine = ui.el('p', {
      class: 'settings__status',
      role: 'status',
      attrs: { 'aria-live': 'polite' },
      i18nAttr: 'aria-label:settings.status.label'
    });
    body = ui.el('div', { class: 'settings__body' });
    host.appendChild(ui.el('div', { class: 'settings' }, [statusLine, body]));

    /* Registered here and not in enter(): ADC.app.onSettings has no unsubscribe
       (app.js:146-150), so a registration per visit would stack one listener per
       navigation. */
    ADC.app.onSettings(onExternalChange);
  }

  /*
   * actions is the topbar container, already emptied by app.js. It stays empty: every control
   * here writes on change, so a topbar "save" would imply the fields are not saved yet, and a
   * "reload" would compete with the retry the failure panel already carries.
   */
  function enter(hostEl, actions) {
    active = true;
    if (hostEl) { host = hostEl; }
    clearStatus();
    render();
    /* Both are cached in app.js, so this is a no-op from the second visit on, and each one
       re-renders when it lands rather than holding the form back on a round trip. */
    if (presetNames === null) { loadCatalog(); }
    if (volumeRows === null && !volumesFailed) { loadVolumes(false); }
    if (updateState.progress && updateState.progress.status === 'downloading') {
      pollUpdateProgress();
    }
    return actions;
  }

  function leave() {
    /* Nothing to stop -- no job, no watch, no poll. What active buys is that a catalogue or
       volume promise landing after the user navigated away does not rebuild a section nobody
       is looking at, and does not pull focus back into it. */
    active = false;
    clearStatus();
    stopUpdatePoll();
  }

  function relang() {
    /* Re-render rather than trust i18n.apply(): apply() relabels a data-i18n node, but it
       cannot re-pick a select's option, cannot reorder anything, and cannot reformat the
       exclusion count -- that string was built here with tn and fmtInt. */
    render();
  }

  views.settings = {
    mount: mount,
    enter: enter,
    leave: leave,
    relang: relang
  };
})();
