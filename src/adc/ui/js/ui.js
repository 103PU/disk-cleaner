/*
 * ui.js -- ADC.ui. Shared DOM construction, so seven views build one UI.
 *
 * This file exists for a specific failure mode. Seven view files each needing a card,
 * a risk badge, a byte figure and a disabled-with-a-reason checkbox will produce seven
 * near-identical implementations, three of which will forget something the spec
 * requires: the "≥" on a truncated size, the text label next to the risk colour, the
 * tooltip on an admin-gated row. Those are not stylistic details -- they are
 * docs/02-SPEC.md 7.3 and 7.4, and they are acceptance rows in docs/03-PLAN.md. So the
 * components that carry them live here, once, and a view calls them.
 *
 * Two rules hold everywhere below:
 *
 *   textContent, never innerHTML. Not once, not for "trusted" strings. Target names,
 *   paths, log lines and error messages all come from a machine's filesystem, and a
 *   folder can be called anything at all.
 *
 *   No `opacity` on text. Dimmed text uses --text-muted or --text-dim, which have
 *   measured contrast ratios; composited opacity does not (see css/tokens.css).
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var i18n = ADC.i18n;

  /* The four risk tiers, in the engine's own order (targets.py:72-78). Each carries a
     distinct icon SHAPE, because docs/02-SPEC.md 7.3 forbids conveying the tier by
     colour alone -- a colour-blind user reads the shape and the label. */
  var RISK = {
    safe: { icon: 'icon-safe', key: 'risk.safe' },
    rebuildable: { icon: 'icon-rebuildable', key: 'risk.rebuildable' },
    caution: { icon: 'icon-caution', key: 'risk.caution' },
    dangerous: { icon: 'icon-dangerous', key: 'risk.dangerous' }
  };

  /* Category icons, for the group headers in the Clean view. */
  var CATEGORY_ICON = {
    dev: 'icon-projects', browser: 'icon-search', ide: 'icon-projects',
    ai_ml: 'icon-brand', system: 'icon-settings', temp: 'icon-clean',
    vm: 'icon-disk', app: 'icon-folder'
  };

  var SVG_NS = 'http://www.w3.org/2000/svg';
  /* --- element construction --------------------------------------------------- */

  /*
   * el('div', {class: 'card', text: 'hi'}, [child, child])
   *
   * A hyperscript small enough to read in one sitting, because the alternative in a
   * CSP that forbids inline anything is string-building HTML and assigning innerHTML,
   * which is the one thing this UI must never do.
   *
   * Recognised props:
   *   class / cls   className
   *   text          textContent (a number is accepted and stringified)
   *   i18n          data-i18n key; also sets the text immediately
   *   i18nAttr      data-i18n-attr value, e.g. 'title:foo,aria-label:bar'
   *   on            {click: fn, change: fn, ...}
   *   attrs         {'aria-label': 'x', 'data-id': 'y'}
   *   hidden/disabled/checked/value/type/id/title/role/tabIndex  set as properties
   * Anything else is set as an attribute, so `el('input', {min: 0})` works.
   */
  function el(tag, props, children) {
    var node = document.createElement(tag);
    var p = props || {};
    var key;
    for (key in p) {
      if (!Object.prototype.hasOwnProperty.call(p, key)) { continue; }
      var v = p[key];
      if (v === null || v === undefined) { continue; }
      if (key === 'class' || key === 'cls') { node.className = v; }
      else if (key === 'text') { node.textContent = String(v); }
      else if (key === 'i18n') {
        node.setAttribute('data-i18n', v);
        node.textContent = i18n.t(v);
      } else if (key === 'i18nAttr') {
        node.setAttribute('data-i18n-attr', v);
      } else if (key === 'on') {
        for (var evt in v) {
          if (Object.prototype.hasOwnProperty.call(v, evt)) { node.addEventListener(evt, v[evt]); }
        }
      } else if (key === 'attrs') {
        for (var a in v) {
          if (Object.prototype.hasOwnProperty.call(v, a)) { node.setAttribute(a, String(v[a])); }
        }
      } else if (key in node) { node[key] = v; }
      else { node.setAttribute(key, String(v)); }
    }
    if (p.i18nAttr) { i18n.apply(node); }
    append(node, children);
    return node;
  }
  /* Accepts a node, a string, an array, or null -- so a caller can write
     `[maybeNode && row, 'text']` without filtering first. */
  function append(parent, children) {
    if (children === null || children === undefined || children === false) { return parent; }
    if (Array.isArray(children)) {
      for (var i = 0; i < children.length; i += 1) { append(parent, children[i]); }
      return parent;
    }
    if (typeof children === 'string' || typeof children === 'number') {
      parent.appendChild(document.createTextNode(String(children)));
      return parent;
    }
    if (children.nodeType) { parent.appendChild(children); }
    return parent;
  }

  function clear(node) {
    while (node && node.firstChild) { node.removeChild(node.firstChild); }
    return node;
  }

  /*
   * <svg><use href="#icon-x"/></svg> against the sprite in index.html.
   *
   * createElementNS, not innerHTML, and `href` rather than `xlink:href` -- WebView2 is
   * Chromium, so the modern attribute works and the deprecated one is not needed.
   * aria-hidden on every icon: an icon that means something has a text label beside it,
   * and an icon that stands alone is inside a button that carries an aria-label. There
   * is no icon in this UI that a screen reader should read.
   */
  function icon(name, cls) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', cls ? 'ico ' + cls : 'ico');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  /* A span whose text is set later by the caller keeping the reference. Saves every
     view writing `var x = el('span'); ... x.textContent = ...`. */
  function span(cls, text) {
    return el('span', { class: cls, text: text === undefined ? '' : text });
  }
  /* --- buttons ---------------------------------------------------------------- */

  /*
   * btn({i18n: 'clean.run', icon: 'icon-play', variant: 'primary', on: {click: f}})
   *
   * type="button" always. A <button> inside anything form-shaped defaults to submit,
   * and a submit here would try to navigate a file:// document -- form-action 'none' in
   * the CSP would block it, but the visible symptom would be "the button does nothing"
   * and the cause would be three layers away.
   *
   * An icon-only button REQUIRES `label` (an i18n key): it becomes aria-label and
   * title, which is docs/02-SPEC.md 7.4's "aria-label cho mọi icon button". Passing
   * neither text nor label produces a button no screen reader can announce, so that
   * combination throws during development rather than shipping silently.
   */
  function btn(opts) {
    var o = opts || {};
    var classes = ['btn'];
    if (o.variant) { classes.push('btn--' + o.variant); }
    if (o.block) { classes.push('btn--block'); }
    if (o.size) { classes.push('btn--' + o.size); }
    if (o.class) { classes.push(o.class); }
    var hasText = !!(o.i18n || o.text);
    if (!hasText && !o.label) {
      throw new Error('ui.btn: an icon-only button needs label: <i18n key> for aria-label');
    }
    var node = el('button', {
      class: classes.join(' '),
      type: 'button',
      disabled: o.disabled === true,
      on: o.on
    });
    if (o.icon) { node.appendChild(icon(o.icon, 'btn__ico')); }
    if (o.i18n) { node.appendChild(el('span', { i18n: o.i18n })); }
    else if (o.text) { node.appendChild(span('', o.text)); }
    if (o.label) {
      node.setAttribute('data-i18n-attr', 'aria-label:' + o.label + ',title:' + o.label);
      i18n.apply(node);
    } else if (o.title) {
      node.title = o.title;
    }
    if (!hasText) { node.classList.add('btn--icon'); }
    return node;
  }
  /* --- panels ----------------------------------------------------------------- */

  /*
   * card({i18n: 'overview.disks', sub: 'overview.disks.sub', actions: [btn]})
   *
   * Returns the outer <section> with `.body` hung off it as a property, so a caller
   * fills the body without querySelector-ing back into its own DOM.
   */
  function card(opts) {
    var o = opts || {};
    var body = el('div', { class: 'card__body' });
    var head = null;
    if (o.i18n || o.title || o.actions) {
      var titles = el('div', { class: 'card__titles' }, [
        o.i18n ? el('h2', { class: 'card__title', i18n: o.i18n })
               : (o.title ? el('h2', { class: 'card__title', text: o.title }) : null),
        o.sub ? el('p', { class: 'card__sub', i18n: o.sub })
              : (o.subText ? el('p', { class: 'card__sub', text: o.subText }) : null)
      ]);
      head = el('header', { class: 'card__head' }, [
        o.icon ? icon(o.icon, 'card__ico') : null,
        titles,
        o.actions ? el('div', { class: 'card__actions' }, o.actions) : null
      ]);
    }
    var node = el('section', { class: o.class ? 'card ' + o.class : 'card' }, [head, body]);
    node.body = body;
    node.head = head;
    return node;
  }

  /* One big number with a label under it. `hint` is the small print that stops a number
     from lying -- "≥" cases, the Docker note, "not measurable". */
  function stat(opts) {
    var o = opts || {};
    var value = el('div', { class: 'stat__value num', text: o.value === undefined ? '—' : o.value });
    var node = el('div', { class: o.tone ? 'stat stat--' + o.tone : 'stat' }, [
      value,
      o.i18n ? el('div', { class: 'stat__label', i18n: o.i18n })
             : el('div', { class: 'stat__label', text: o.label || '' }),
      o.hint ? el('p', { class: 'stat__hint', text: o.hint }) : null
    ]);
    node.value = value;
    return node;
  }
  /* --- risk, sizes, admin ------------------------------------------------------ */

  /*
   * The risk badge. Icon + text label + colour, in that order of importance.
   *
   * docs/02-SPEC.md 7.3: "Tầng rủi ro không chỉ thể hiện bằng màu: mỗi tầng có icon +
   * nhãn chữ." So this always renders the word. A `compact` badge shrinks the text, it
   * does not drop it -- there is no variant of this component that is colour-only, and
   * that is the point of it being a component.
   */
  function riskBadge(risk, opts) {
    var o = opts || {};
    var meta = RISK[risk] || { icon: 'icon-info', key: 'risk.unknown' };
    var classes = ['risk', 'risk--' + (RISK[risk] ? risk : 'unknown')];
    if (o.compact) { classes.push('risk--compact'); }
    return el('span', { class: classes.join(' ') }, [
      icon(meta.icon, 'risk__ico'),
      el('span', { class: 'risk__text', i18n: meta.key })
    ]);
  }

  /*
   * A size, with the honesty rules attached.
   *
   * A scan row that hit its budget carries `truncated: true` and knows only a lower
   * bound; a row the engine cannot measure carries `measurable: false` and has no
   * number at all. Both are common -- WinSxS is the first, `docker_vhdx_compact` and
   * `vss_manage` are the second -- and both were shown as a confident figure in v1,
   * which is BUG-07. Passing the row itself rather than a number is what makes it hard
   * to forget: the flags travel with the value.
   */
  function size(row, field) {
    var key = field || 'size';
    if (row && row.measurable === false) {
      return el('span', { class: 'size size--none', i18n: 'size.not_measurable' });
    }
    var n = row ? row[key] : null;
    if (typeof n !== 'number') {
      return el('span', { class: 'size size--none', text: '—' });
    }
    var truncated = !!(row && row.truncated);
    var node = el('span', {
      class: truncated ? 'size size--lower-bound num' : 'size num',
      text: i18n.fmtBytes(n, { truncated: truncated })
    });
    if (truncated) {
      node.setAttribute('data-i18n-attr', 'title:size.truncated_hint');
      i18n.apply(node);
    }
    return node;
  }
  /* A little pill for "needs Administrator", "irreversible", "standalone", "cached".
     Text always; the icon is optional decoration. */
  function tag(opts) {
    var o = opts || {};
    var classes = ['tag'];
    if (o.tone) { classes.push('tag--' + o.tone); }
    var node = el('span', { class: classes.join(' ') }, [
      o.icon ? icon(o.icon, 'tag__ico') : null,
      o.i18n ? el('span', { i18n: o.i18n }) : span('', o.text || '')
    ]);
    if (o.title) {
      node.setAttribute('data-i18n-attr', 'title:' + o.title);
      i18n.apply(node);
    }
    return node;
  }

  /*
   * A checkbox row for one target. The single most spec-loaded component in the UI, so
   * it is here rather than in the Clean view:
   *
   *   - `disabled` + `reason` renders the tooltip AND a visible lock tag. v1 let an
   *     admin-gated row be ticked and then failed silently at run time (docs/01-AUDIT
   *     BUG-04); "disabled with a stated reason" is docs/02-SPEC.md 7.3.
   *   - the label is wired with `for`/`id`, so the whole text is a click target and a
   *     screen reader announces the name rather than "checkbox".
   *   - aria-describedby points at the description line, so the risk tier and the
   *     rebuild cost are announced with the row instead of being visual-only.
   */
  function targetRow(opts) {
    var o = opts || {};
    var id = 'tgt-' + o.id;
    var box = el('input', {
      type: 'checkbox',
      id: id,
      class: 'trow__box',
      checked: o.checked === true,
      disabled: o.disabled === true,
      value: o.id,
      on: o.on
    });
    if (o.disabled && o.reason) { box.setAttribute('aria-describedby', id + '-why'); }
    var right = el('div', { class: 'trow__right' }, o.right || null);
    var meta = el('div', { class: 'trow__meta' }, o.meta || null);
    var classes = ['trow'];
    if (o.disabled) { classes.push('trow--disabled'); }
    if (o.risk) { classes.push('trow--' + o.risk); }
    var node = el('div', { class: classes.join(' '), attrs: { 'data-target-id': o.id } }, [
      box,
      el('div', { class: 'trow__main' }, [
        el('label', { class: 'trow__name', attrs: { for: id }, text: o.name || o.id }),
        el('p', { class: 'trow__desc', text: o.desc || '' }),
        meta,
        o.disabled && o.reason
          ? el('p', { class: 'trow__why', id: id + '-why', text: o.reason })
          : null,
        /* The Docker note and its kin: an estimate that is a file size, not a
           reclaimable amount. Shown next to the number, in words, always. */
        o.note ? el('p', { class: 'trow__note', text: o.note }) : null
      ]),
      right
    ]);
    node.box = box;
    node.right = right;
    node.metaBox = meta;
    return node;
  }

  /* --- progress ---------------------------------------------------------------- */

  /*
   * A determinate bar that degrades to indeterminate. The engine reports `pct` for
   * phases it can count and leaves it at 0 for a walk whose size it does not yet know;
   * a bar frozen at 0 for forty seconds reads as "hung", so a missing pct switches to
   * the striped indeterminate style instead of lying about progress.
   */
  function progress() {
    var fill = el('div', { class: 'bar__fill' });
    var bar = el('div', {
      class: 'bar',
      role: 'progressbar',
      attrs: { 'aria-valuemin': '0', 'aria-valuemax': '100' }
    }, [fill]);
    var label = el('p', { class: 'bar__label' });
    var node = el('div', { class: 'progress' }, [bar, label]);
    node.set = function (pct, text) {
      var known = typeof pct === 'number' && pct > 0;
      bar.classList.toggle('bar--indeterminate', !known);
      fill.style.width = known ? Math.min(100, Math.max(0, pct)) + '%' : '100%';
      if (known) { bar.setAttribute('aria-valuenow', String(Math.round(pct))); }
      else { bar.removeAttribute('aria-valuenow'); }
      label.textContent = text || '';
      return node;
    };
    return node.set(0, '');
  }
  /* --- console ----------------------------------------------------------------- */

  /* Event levels, from jobs.py:38-42. */
  var LEVELS = ['info', 'success', 'warn', 'error'];

  /* docs/02-SPEC.md 7.5's cap. The full log is on disk regardless -- audit.py writes
     every line -- so trimming the DOM loses nothing but keeps a cancelled walk over a
     million files from turning the renderer into a memory sink. */
  var CONSOLE_MAX = 5000;

  /*
   * The console pane. v1's users liked it and it stays, with the four things it was
   * missing (docs/02-SPEC.md 7.5): a level filter, a button to the real log file, the
   * DOM cap above, and an accessible live region.
   *
   * role="log" + aria-live="polite" rather than "assertive": a scan emits lines faster
   * than speech, and assertive would interrupt itself continuously. Polite means a
   * screen reader finishes the sentence it is on.
   *
   * Returns the node with .push(events), .clear(), .setBusy(bool).
   */
  function consolePane(opts) {
    var o = opts || {};
    var filter = 'all';
    var lines = [];

    var list = el('div', {
      class: 'log__list',
      role: 'log',
      attrs: { 'aria-live': 'polite', 'aria-relevant': 'additions', tabindex: '0' },
      i18nAttr: 'aria-label:log.label'
    });

    function visible(level) {
      return filter === 'all' || filter === level;
    }

    function render(entry) {
      var node = el('p', { class: 'log__line log__line--' + entry.level }, [
        el('span', { class: 'log__ts num', text: i18n.fmtClock(entry.ts) }),
        icon(entry.level === 'error' ? 'icon-dangerous'
          : entry.level === 'warn' ? 'icon-caution'
          : entry.level === 'success' ? 'icon-safe' : 'icon-info', 'log__ico'),
        el('span', { class: 'log__text', text: i18n.pickField(entry, 'message') })
      ]);
      node.hidden = !visible(entry.level);
      entry.node = node;
      return node;
    }
    /* Only autoscroll when the user is already at the bottom. Someone who has scrolled
       up to read a warning is reading it; yanking them back down every 250 ms is the
       behaviour that makes a live console unusable. */
    function atBottom() {
      return list.scrollHeight - list.scrollTop - list.clientHeight < 24;
    }

    function push(events) {
      if (!events || !events.length) { return; }
      var stick = atBottom();
      var frag = document.createDocumentFragment();
      for (var i = 0; i < events.length; i += 1) {
        var entry = events[i];
        lines.push(entry);
        frag.appendChild(render(entry));
      }
      list.appendChild(frag);
      /* Trim from the front, in one pass, only when over. */
      if (lines.length > CONSOLE_MAX) {
        var excess = lines.length - CONSOLE_MAX;
        for (var j = 0; j < excess; j += 1) {
          if (lines[j].node && lines[j].node.parentNode) {
            list.removeChild(lines[j].node);
          }
        }
        lines = lines.slice(excess);
      }
      if (stick) { list.scrollTop = list.scrollHeight; }
    }

    /* Filtering hides rather than rebuilds: the lines are already in the DOM, the
       filter is toggled repeatedly, and a rebuild of five thousand nodes to show four
       hundred of them is work for nothing. */
    function setFilter(level) {
      filter = level;
      for (var i = 0; i < lines.length; i += 1) {
        if (lines[i].node) { lines[i].node.hidden = !visible(lines[i].level); }
      }
      for (var b = 0; b < buttons.length; b += 1) {
        var on = buttons[b].getAttribute('data-level') === level;
        buttons[b].classList.toggle('chip--on', on);
        buttons[b].setAttribute('aria-pressed', on ? 'true' : 'false');
      }
    }

    function chip(level) {
      var b = el('button', {
        class: 'chip',
        type: 'button',
        i18n: 'log.level.' + level,
        attrs: { 'data-level': level, 'aria-pressed': 'false' },
        on: { click: function () { setFilter(level); } }
      });
      return b;
    }
    var buttons = [chip('all')];
    for (var L = 0; L < LEVELS.length; L += 1) { buttons.push(chip(LEVELS[L])); }

    var bar = el('div', { class: 'log__bar' }, [
      el('div', { class: 'log__filters', role: 'group', i18nAttr: 'aria-label:log.filter' },
        buttons),
      el('div', { class: 'log__tools' }, [
        /* "Mở file log" (docs/02-SPEC.md 7.5). The bridge method takes no argument at
           all -- the page cannot name a file -- so this button is a bare call. */
        btn({
          i18n: 'log.open_file', icon: 'icon-log', variant: 'ghost', size: 'sm',
          on: {
            click: function () {
              ADC.api.openLog().then(function (data) {
                toast(i18n.t('log.opened', { path: data.opened }), { kind: 'info' });
              }, showError);
            }
          }
        }),
        btn({
          i18n: 'log.clear', icon: 'icon-close', variant: 'ghost', size: 'sm',
          on: { click: function () { node.clear(); } }
        })
      ])
    ]);

    var node = el('section', { class: 'log' }, [
      el('header', { class: 'log__head' }, [
        el('h2', { class: 'log__title', i18n: o.titleKey || 'log.title' }),
        el('span', { class: 'log__spin', hidden: true }, [icon('icon-refresh', 'spin')])
      ]),
      bar,
      list
    ]);

    node.push = push;
    node.setFilter = setFilter;
    node.clear = function () {
      lines = [];
      clear(list);
    };
    node.setBusy = function (busy) {
      node.querySelector('.log__spin').hidden = !busy;
    };
    /* Re-render on a language switch: every line holds both strings, so this is a
       relabel and not a refetch. */
    i18n.onChange(function () {
      var kept = lines;
      node.clear();
      push(kept);
    });
    setFilter('all');
    return node;
  }
  /* --- transient messages ------------------------------------------------------- */

  var TOAST_MS = 6000;
  var TOAST_MS_ERROR = 12000;

  /*
   * A toast, into the live region in index.html. Errors stay twice as long and carry a
   * close button, because an error the user did not finish reading is an error they
   * will report as "it just did nothing".
   */
  function toast(message, opts) {
    var o = opts || {};
    var host = document.getElementById('toast-host');
    if (!host) { return null; }
    var kind = o.kind || 'info';
    var node = el('div', { class: 'toast toast--' + kind }, [
      icon(kind === 'error' ? 'icon-dangerous'
        : kind === 'warn' ? 'icon-caution'
        : kind === 'success' ? 'icon-safe' : 'icon-info', 'toast__ico'),
      el('div', { class: 'toast__body' }, [
        el('p', { class: 'toast__text', text: message }),
        o.detail ? el('p', { class: 'toast__detail mono', text: o.detail }) : null
      ]),
      btn({
        icon: 'icon-close', label: 'action.dismiss', variant: 'bare', size: 'sm',
        on: { click: function () { remove(); } }
      })
    ]);
    function remove() {
      if (node.parentNode) { node.parentNode.removeChild(node); }
    }
    host.appendChild(node);
    window.setTimeout(remove, kind === 'error' ? TOAST_MS_ERROR : TOAST_MS);
    return node;
  }

  /*
   * The one error handler every view should hand to a rejected promise:
   * `ADC.api.scanStart(...).catch(ADC.ui.showError)`.
   *
   * An ApiError already carries a localised sentence written by the engine, so this
   * shows it verbatim. Anything else is a bug in this page rather than a refusal from
   * the engine -- those get a generic line plus the JS message as detail, and are
   * logged to the console where a developer will find them.
   */
  function showError(err) {
    if (err && err.name === 'ApiError') {
      toast(err.text(), { kind: 'error', detail: err.code });
    } else {
      if (window.console) { console.error(err); }
      toast(i18n.t('error.unexpected'), {
        kind: 'error',
        detail: err && err.message ? String(err.message) : String(err)
      });
    }
    return null;
  }
  /* --- empty states -------------------------------------------------------------- */

  /*
   * `i18n` and `body` are dictionary keys; `text` is free text, for the cases where the
   * interesting part of the message is not translatable -- a view name, a path, or a
   * sentence the engine already localised itself and handed over as a string.
   */
  function emptyState(opts) {
    var o = opts || {};
    return el('div', { class: 'empty' }, [
      icon(o.icon || 'icon-info', 'empty__ico'),
      el('h3', { class: 'empty__title', i18n: o.i18n }),
      o.body ? el('p', { class: 'empty__body', i18n: o.body }) : null,
      o.text ? el('p', { class: 'empty__detail', text: o.text }) : null,
      o.action ? el('div', { class: 'empty__action' }, o.action) : null
    ]);
  }

  /*
   * The honest placeholder for a view whose engine does not exist yet.
   *
   * Disk Explorer, Project Sweeper and Schedule are P5 (docs/03-PLAN.md), and their
   * sidebar entries are in the shell from P4 because removing and re-adding them later
   * would churn the nav and the i18n keys. What goes in the panel is a plain statement
   * that the feature arrives in P5 and a list of what it will do -- not a mock, not a
   * disabled fake button, and not a spinner that never resolves. A fake UI in a
   * half-built app is indistinguishable from a broken one.
   */
  function comingSoon(opts) {
    var o = opts || {};
    return el('div', { class: 'soon' }, [
      icon('icon-soon', 'soon__ico'),
      el('h3', { class: 'soon__title', i18n: o.i18n }),
      el('p', { class: 'soon__phase', i18n: 'soon.phase' }),
      o.body ? el('p', { class: 'soon__body', i18n: o.body }) : null,
      o.items && o.items.length
        ? el('ul', { class: 'soon__list' }, o.items.map(function (key) {
          return el('li', { class: 'soon__item' }, [
            icon('icon-chevron', 'soon__bullet'),
            el('span', { i18n: key })
          ]);
        }))
        : null
    ]);
  }

  /* A labelled row for the Settings view: label + control + optional help text. */
  function field(opts) {
    var o = opts || {};
    var control = o.control;
    if (control && o.id) { control.id = o.id; }
    return el('div', { class: 'field' }, [
      el('label', { class: 'field__label', i18n: o.i18n, attrs: o.id ? { for: o.id } : null }),
      el('div', { class: 'field__control' }, [
        control,
        o.help ? el('p', { class: 'field__help', i18n: o.help }) : null
      ])
    ]);
  }
  /* --- modal confirm -------------------------------------------------------------- */

  /*
   * confirm({i18n, body, confirmKey, danger, lines, phrase}) -> Promise
   *
   * Not window.confirm, for two reasons that both matter here. It cannot show the
   * preview -- and a clean confirmation that does not state the figure and the row
   * count is not a confirmation of anything (docs/02-SPEC.md 4.6). And a native dialog
   * in a WebView2 window is modal to the whole process, so a scan running on a worker
   * thread would keep going with the UI frozen behind it.
   *
   * Resolves false when declined and true when accepted -- except with `phrase`, where
   * accepting resolves the TEXT the user typed, so the caller can send it on to the
   * engine that will check it again. Every caller tests truthiness, and no accepted
   * phrase is empty, so the two shapes are used identically.
   *
   * `phrase` is docs/02-SPEC.md 4.3's DANGEROUS tier: "phải gõ chữ xác nhận". The
   * confirm button stays disabled until the box matches, and the match here is
   * deliberately STRICTER than the engine's (adc/engine/vss.py PendingAction.matches
   * forgives a trailing colon or backslash) -- narrower means the button never lights
   * up for something the engine would then refuse, and the rule about what counts as
   * typing the phrase stays defined in exactly one place: Python.
   *
   * Focus goes to the SAFER button on open -- cancel for a dangerous action, or the
   * empty phrase box, whose Enter does nothing while the button is disabled -- and Esc
   * resolves false. Focus returns to whatever opened it, because a modal that drops
   * focus on the body leaves a keyboard user at the top of the document.
   */
  function confirm(opts) {
    var o = opts || {};
    var opener = document.activeElement;
    return new Promise(function (resolve) {
      var done = false;
      function finish(answer) {
        if (done) { return; }
        done = true;
        document.removeEventListener('keydown', onKey, true);
        if (overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
        if (opener && opener.focus) { opener.focus(); }
        resolve(answer);
      }
      function onKey(evt) {
        if (evt.key === 'Escape') { evt.preventDefault(); finish(false); return; }
        /* A modal that lets Tab escape into the page behind it is not modal. */
        if (evt.key === 'Tab') {
          var focusable = box.querySelectorAll('button, [href], input, select, textarea');
          if (!focusable.length) { return; }
          var first = focusable[0];
          var last = focusable[focusable.length - 1];
          if (evt.shiftKey && document.activeElement === first) {
            evt.preventDefault(); last.focus();
          } else if (!evt.shiftKey && document.activeElement === last) {
            evt.preventDefault(); first.focus();
          }
        }
      }
      var cancelBtn = btn({
        i18n: o.cancelKey || 'action.cancel', variant: 'ghost',
        on: { click: function () { finish(false); } }
      });
      /*
       * The typed-phrase gate. `accept()` re-checks instead of trusting the button's
       * disabled attribute, because that attribute is one devtools click away from
       * gone -- and the engine re-checks after that, which is the check that counts.
       */
      var wanted = typeof o.phrase === 'string' ? o.phrase.trim() : '';
      function matches() {
        return !wanted || typed.value.trim().toUpperCase() === wanted.toUpperCase();
      }
      function sync() { okBtn.disabled = !matches(); }
      function accept() { if (matches()) { finish(wanted ? typed.value.trim() : true); } }
      var typed = wanted ? el('input', {
        class: 'dialog__phrase', type: 'text', id: 'dlg-phrase',
        attrs: { autocomplete: 'off', spellcheck: 'false' },
        on: {
          input: sync,
          /* Enter submits only what the box already earned: the button is disabled
             until the phrase matches, so this cannot become a one-keystroke delete. */
          keydown: function (evt) { if (evt.key === 'Enter') { evt.preventDefault(); accept(); } }
        }
      }) : null;
      var okBtn = btn({
        i18n: o.confirmKey || 'action.confirm',
        variant: o.danger ? 'danger' : 'primary',
        icon: o.danger ? 'icon-dangerous' : null,
        disabled: !!wanted,
        on: { click: accept }
      });
      var box = el('div', {
        class: o.danger ? 'dialog dialog--danger' : 'dialog',
        role: 'alertdialog',
        attrs: { 'aria-modal': 'true', 'aria-labelledby': 'dlg-title' }
      }, [
        el('header', { class: 'dialog__head' }, [
          o.danger ? icon('icon-dangerous', 'dialog__ico') : null,
          el('h2', { class: 'dialog__title', id: 'dlg-title', i18n: o.i18n })
        ]),
        el('div', { class: 'dialog__body' }, [
          o.body ? el('p', { class: 'dialog__text', i18n: o.body }) : null,
          o.text ? el('p', { class: 'dialog__text', text: o.text }) : null,
          /* Free-text lines: the preview figures. Plain text nodes, because a folder
             name is arbitrary and this is a confirmation dialog, of all places, to get
             injection wrong in. */
          o.lines && o.lines.length
            ? el('ul', { class: 'dialog__lines' }, o.lines.map(function (line) {
              return el('li', { class: 'dialog__line', text: line });
            }))
            : null,
          o.note ? el('p', { class: 'dialog__note', text: o.note }) : null,
          typed ? el('div', { class: 'dialog__ask' }, [
            el('label', {
              class: 'dialog__asklabel', attrs: { for: 'dlg-phrase' },
              text: i18n.t('dialog.phrase', { phrase: wanted })
            }),
            typed
          ]) : null
        ]),
        el('footer', { class: 'dialog__foot' }, [cancelBtn, okBtn])
      ]);
      var overlay = el('div', {
        class: 'overlay',
        on: {
          /* Click-outside cancels, but only on the backdrop itself -- a drag that ends
             outside the box must not count as a decision. */
          mousedown: function (evt) { if (evt.target === overlay) { finish(false); } }
        }
      }, [box]);
      document.body.appendChild(overlay);
      document.addEventListener('keydown', onKey, true);
      (typed || (o.danger ? cancelBtn : okBtn)).focus();
    });
  }

  /* The bridge-is-gone dialog from index.html. Nothing recovers from this, so it stays
     up: the only fix is restarting the app. */
  function fatal(detail) {
    var box = document.getElementById('fatal');
    if (!box) { return; }
    var line = document.getElementById('fatal-detail');
    if (line) { line.textContent = detail ? String(detail) : ''; }
    box.hidden = false;
  }
  ADC.ui = {
    RISK: RISK,
    CATEGORY_ICON: CATEGORY_ICON,
    LEVELS: LEVELS,
    CONSOLE_MAX: CONSOLE_MAX,

    el: el,
    append: append,
    clear: clear,
    icon: icon,
    span: span,

    btn: btn,
    card: card,
    stat: stat,
    tag: tag,
    field: field,
    riskBadge: riskBadge,
    size: size,
    targetRow: targetRow,
    progress: progress,
    consolePane: consolePane,
    emptyState: emptyState,
    comingSoon: comingSoon,

    toast: toast,
    showError: showError,
    confirm: confirm,
    fatal: fatal
  };
})();
