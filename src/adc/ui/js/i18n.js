/*
 * i18n.js -- ADC.i18n. Chrome strings, number and date formatting, live switching.
 *
 * Two things make this smaller than an i18n layer usually is.
 *
 * First, the engine is already bilingual: every catalogue row carries
 * `name: {vi, en}` and `desc: {vi, en}`, every job event carries `detail_vi` and
 * `detail_en`, and every refusal from the bridge carries `message_vi` and
 * `message_en`. So the dictionaries here cover the chrome only -- nav labels, button
 * text, table headers -- and `pick()` / `pickField()` take the right half off an
 * engine object. Duplicating 49 target descriptions into a JS dictionary would create
 * a second place for them to drift.
 *
 * Second, the dictionaries are plain flat objects with dotted string keys, not nested
 * ones. `t('nav.overview')` is one property lookup, and -- the actual reason --
 * `data-i18n="nav.overview"` in index.html is greppable against Object.keys(), which
 * is how the i18n coverage test proves no key is missing and no key is dead.
 *
 * They are .js and not .json because of the CSP: connect-src 'none' on a file://
 * origin means fetch() cannot load anything. See index.html's head comment.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var locales = ADC.locales = ADC.locales || {};

  var FALLBACK = 'en';
  var DEFAULT = 'vi';
  var SUPPORTED = ['vi', 'en'];

  /* Intl tags, kept apart from the dictionary keys: 'vi' names the dictionary,
     'vi-VN' names the number and date rules. */
  var INTL = { vi: 'vi-VN', en: 'en-US' };

  var lang = DEFAULT;
  var listeners = [];

  /* Keys asked for and not found, in ask order. Nothing reads this at runtime; it is
     here so a dev-console poke (`ADC.i18n.missing`) answers "what did I forget" after
     clicking through every view, and so a test can assert it stayed empty. */
  var missing = [];
  function dict(which) {
    return locales[which] || {};
  }

  /* {name} substitution. Deliberately not a template evaluator: the values come from
     the engine and from user settings, and this writes into textContent, so the only
     thing a placeholder can do is put a string where the brace was. */
  function fill(text, vars) {
    if (!vars) { return text; }
    return text.replace(/\{(\w+)\}/g, function (whole, key) {
      return Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : whole;
    });
  }

  /* Current language, then English, then the key itself. Returning the key rather
     than an empty string is deliberate: a missing string shows up as
     `history.empty.title` in the window, which is unmistakable, where "" reads as a
     layout bug and gets chased in the CSS. */
  function t(key, vars) {
    var hit = dict(lang)[key];
    if (hit === undefined) {
      hit = dict(FALLBACK)[key];
      if (hit === undefined) {
        if (missing.indexOf(key) === -1) { missing.push(key); }
        return key;
      }
    }
    return fill(String(hit), vars);
  }

  /* Count-aware lookup: `tn('clean.selected', 3)` tries `clean.selected.other` then
     `clean.selected`. Vietnamese has no plural agreement so vi.js normally defines
     only the base key; English defines `.one` and `.other` where it matters. */
  function tn(key, n, vars) {
    var suffix = n === 1 ? '.one' : '.other';
    var vals = { n: fmtInt(n) };
    if (vars) {
      for (var k in vars) {
        if (Object.prototype.hasOwnProperty.call(vars, k)) { vals[k] = vars[k]; }
      }
    }
    var candidate = key + suffix;
    if (dict(lang)[candidate] !== undefined || dict(FALLBACK)[candidate] !== undefined) {
      return t(candidate, vals);
    }
    return t(key, vals);
  }
  /* --- engine strings ------------------------------------------------------- */

  /* `pick(row.name)` -- takes {vi, en} off an engine object. Falls back to the other
     language rather than showing nothing, because a catalogue row with a Vietnamese
     name and no English one should still be readable in English. */
  function pick(obj) {
    if (obj === null || obj === undefined) { return ''; }
    if (typeof obj === 'string') { return obj; }
    var mine = obj[lang];
    if (mine !== undefined && mine !== null && mine !== '') { return String(mine); }
    var other = obj[lang === 'vi' ? 'en' : 'vi'];
    return other === undefined || other === null ? '' : String(other);
  }

  /* `pickField(event, 'detail')` -- the same idea for the flat `detail_vi` /
     `detail_en` pairs that job snapshots and bridge errors use. */
  function pickField(obj, base) {
    if (!obj) { return ''; }
    var mine = obj[base + '_' + lang];
    if (mine !== undefined && mine !== null && mine !== '') { return String(mine); }
    var other = obj[base + '_' + (lang === 'vi' ? 'en' : 'vi')];
    if (other !== undefined && other !== null && other !== '') { return String(other); }
    var plain = obj[base];
    return plain === undefined || plain === null ? '' : String(plain);
  }

  /* --- numbers -------------------------------------------------------------- */

  function nf(digits) {
    return new Intl.NumberFormat(INTL[lang] || INTL.en, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  function fmtInt(n) {
    if (typeof n !== 'number' || !isFinite(n)) { return '—'; }
    return nf(0).format(Math.round(n));
  }

  var UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  /*
   * Bytes, in 1024 steps labelled KB/MB/GB -- Windows' own convention, and v1's, so a
   * number here matches the number Explorer shows for the same folder.
   *
   * Two options exist because two of P4's acceptance rows live in this function:
   *
   *   truncated  a scan that hit its budget knows only a lower bound, so it renders
   *              "≥ 1.74 GB". Printing "1.74 GB" for a partial walk is precisely the
   *              lie BUG-07 was (docs/02-SPEC.md 7.3), and putting the ≥ here rather
   *              than in each view is what stops one view forgetting it.
   *   signed     a free-space delta can legitimately be negative -- another process
   *              can write more than the clean removed -- and "-1.2 GB reclaimed"
   *              must be readable as such rather than shown as a bare 1.2 GB.
   */
  function fmtBytes(n, opts) {
    var o = opts || {};
    if (typeof n !== 'number' || !isFinite(n)) { return '—'; }
    var sign = n < 0 ? '-' : (o.signed && n > 0 ? '+' : '');
    var v = Math.abs(n);
    var i = 0;
    while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i += 1; }
    var digits = i === 0 ? 0 : (v < 10 ? 2 : 1);
    var body = sign + nf(digits).format(v) + ' ' + UNITS[i];
    return o.truncated ? '≥ ' + body : body;
  }

  function fmtPct(x, opts) {
    var o = opts || {};
    if (typeof x !== 'number' || !isFinite(x)) { return '—'; }
    /* The engine hands out free_pct already scaled 0-100, so this does not scale. */
    return nf(o.digits === undefined ? 1 : o.digits).format(x) + '%';
  }

  /* --- time ----------------------------------------------------------------- */

  /* Engine timestamps are epoch SECONDS (time.time()), not milliseconds. Passing one
     straight to `new Date()` would land in 1970 and look like a bug in the report
     writer, so the multiply lives here, once. */
  function toDate(epochSeconds) {
    if (typeof epochSeconds !== 'number' || !isFinite(epochSeconds) || epochSeconds <= 0) {
      return null;
    }
    return new Date(epochSeconds * 1000);
  }
  function fmtDate(epochSeconds, opts) {
    var d = toDate(epochSeconds);
    if (!d) { return '—'; }
    var o = opts || {};
    return d.toLocaleString(INTL[lang] || INTL.en, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
      second: o.seconds ? '2-digit' : undefined
    });
  }

  function fmtClock(epochSeconds) {
    var d = toDate(epochSeconds);
    if (!d) { return '—'; }
    return d.toLocaleTimeString(INTL[lang] || INTL.en, {
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }

  /* Durations are for humans reading a report row, so: sub-minute keeps one decimal
     ("8.2 s"), anything longer drops to whole units ("2 ph 14 gy"). The unit words
     come from the dictionary, because "gy" is not "s". */
  function fmtDuration(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) { return '—'; }
    if (seconds < 60) {
      return nf(seconds < 10 ? 1 : 0).format(seconds) + ' ' + t('unit.second');
    }
    var mins = Math.floor(seconds / 60);
    var secs = Math.round(seconds - mins * 60);
    if (mins < 60) {
      return mins + ' ' + t('unit.minute') + (secs ? ' ' + secs + ' ' + t('unit.second') : '');
    }
    var hours = Math.floor(mins / 60);
    return hours + ' ' + t('unit.hour') + ' ' + (mins - hours * 60) + ' ' + t('unit.minute');
  }

  /* --- applying to the DOM -------------------------------------------------- */

  /*
   * Two attributes, and the split matters for the acceptance check "no hardcoded
   * strings left":
   *
   *   data-i18n="key"                      replaces textContent
   *   data-i18n-attr="title:key,aria-label:other"   replaces named attributes
   *
   * textContent and not innerHTML, everywhere, without exception -- a dictionary is
   * data, and data does not get to inject markup.
   */
  function apply(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
    }
    var attrNodes = scope.querySelectorAll('[data-i18n-attr]');
    for (var j = 0; j < attrNodes.length; j += 1) {
      var el = attrNodes[j];
      var pairs = el.getAttribute('data-i18n-attr').split(',');
      for (var k = 0; k < pairs.length; k += 1) {
        var bits = pairs[k].split(':');
        if (bits.length === 2) {
          el.setAttribute(bits[0].trim(), t(bits[1].trim()));
        }
      }
    }
    return scope;
  }

  /* --- switching ------------------------------------------------------------ */

  function onChange(fn) {
    if (typeof fn === 'function' && listeners.indexOf(fn) === -1) { listeners.push(fn); }
  }

  /*
   * Re-render, not reload. Static markup goes through apply(); anything a view built
   * from engine data has to be rebuilt, because "12,3 GB" and "12.3 GB" are different
   * strings and the view is the only thing that knows where it put them. That is what
   * the listeners are for -- app.js registers one that re-renders the visible view.
   */
  function setLang(next) {
    if (SUPPORTED.indexOf(next) === -1 || next === lang) { return lang; }
    lang = next;
    document.documentElement.lang = lang;
    apply(document);
    for (var i = 0; i < listeners.length; i += 1) {
      try {
        listeners[i](lang);
      } catch (err) {
        /* One view failing to re-render must not leave the other six in the old
           language. Logged, not swallowed silently. */
        if (window.console) { console.error('i18n listener failed', err); }
      }
    }
    return lang;
  }
  ADC.i18n = {
    get lang() { return lang; },
    get missing() { return missing.slice(); },
    supported: SUPPORTED.slice(),
    t: t,
    tn: tn,
    pick: pick,
    pickField: pickField,
    apply: apply,
    setLang: setLang,
    onChange: onChange,
    fmtInt: fmtInt,
    fmtBytes: fmtBytes,
    fmtPct: fmtPct,
    fmtDate: fmtDate,
    fmtClock: fmtClock,
    fmtDuration: fmtDuration,
    toDate: toDate
  };
})();
