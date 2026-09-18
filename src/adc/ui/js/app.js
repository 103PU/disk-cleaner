/*
 * app.js -- boot, navigation, and the small amount of state seven views share.
 *
 * This is the only file in the UI with top-level side effects. Everything else
 * defines something and waits: js/views/*.js each register one object into ADC.views
 * and touch no DOM until called. So the whole start-up order is readable here, in one
 * function, rather than being an emergent property of eight files racing.
 *
 * The view contract, which the seven view files are written against:
 *
 *   ADC.views.<name> = {
 *     mount:  function (host) {}            once, on first visit. host is empty.
 *     enter:  function (host, actions) {}    every visit. `actions` is the cleared
 *                                            topbar container -- append buttons to it.
 *     leave:  function () {}                 on navigating away. STOP POLLING HERE.
 *     relang: function () {}                 after a language switch, if the view drew
 *                                            anything from engine data.
 *   }
 *
 * Only `mount` is required. `leave` is what keeps a scan from polling forever behind
 * a view nobody is looking at, and `relang` is what keeps "12,3 GB" from staying
 * Vietnamese after the switch to English -- ADC.i18n.apply() fixes static markup, but
 * a number a view formatted itself can only be re-formatted by that view.
 *
 * ADC.app below is deliberately small. It holds the four things that are genuinely
 * shared -- settings, the catalogue, the volume list, and the knowledge that a job is
 * already running -- and nothing else. A view wanting to keep its own state keeps it
 * in its own closure.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var i18n = ADC.i18n;
  var api = ADC.api;
  var ui = ADC.ui;
  var views = ADC.views = ADC.views || {};

  /* Sidebar order, and the only names `go()` accepts. Matches index.html's
     data-view attributes and the seven <section id="view-*"> elements. */
  var ORDER = ['overview', 'clean', 'explorer', 'projects', 'schedule', 'history',
    'settings'];
  /*
   * Routes with a <section> and a title but no sidebar entry, reached from inside
   * another view. There is one, and it is one on purpose: docs/02-SPEC.md 5 says the
   * shadow-copy manager "là màn hình riêng, không phải checkbox" -- a real screen, not a
   * dialog -- while an eighth permanent nav item for a screen a user should visit once
   * to undo BUG-09 would sit there forever advertising the most destructive thing in the
   * app. So it is a screen you arrive at from the Clean view's `vss_manage` row.
   */
  var EXTRA = ['vss'];
  var ROUTES = ORDER.concat(EXTRA);
  var FIRST = 'overview';

  var state = {
    current: null,      /* view name on screen */
    mounted: {},        /* name -> true once mount() returned without throwing */
    langAt: {},         /* name -> the language its DOM was last built in */
    settings: null,     /* the live settings object, or null before the first load */
    defaults: null,
    catalogP: null,     /* the catalog() promise, kept so it is fetched once */
    volumesP: null,
    admin: null,
    job: null           /* {kind, owner} while a scan or clean is running */
  };
  var settingsListeners = [];
  function byId(x) { return document.getElementById(x); }

  /* Resolved once in boot(), because every one of these is in index.html and a missing
     one is a typo to find now rather than a null dereference three clicks later. */
  var dom = {};

  function cacheDom() {
    dom.navItems = document.querySelectorAll('.nav__item[data-view]');
    dom.langBtns = document.querySelectorAll('.lang__btn[data-lang]');
    dom.title = byId('view-title');
    dom.sub = byId('view-sub');
    dom.actions = byId('topbar-actions');
    dom.adminBanner = byId('admin-banner');
    dom.adminOk = byId('admin-ok');
    dom.adminCount = byId('admin-count');
    dom.adminRelaunch = byId('admin-relaunch');
    dom.settingsUpdateBadge = byId('settings-update-badge');
    dom.sections = {};
    for (var i = 0; i < ROUTES.length; i += 1) {
      dom.sections[ROUTES[i]] = byId('view-' + ROUTES[i]);
    }
  }

  /* --- shared state --------------------------------------------------------- */

  /*
   * The catalogue is 49 rows that cannot change while the app is running -- they are
   * compiled into the engine, not read from disk -- so it is fetched once and every
   * view gets the same promise. The volume list is the opposite: free space is the
   * number a user watches, so `volumes(true)` re-asks.
   */
  function catalog() {
    if (!state.catalogP) {
      state.catalogP = api.catalog().then(null, function (err) {
        /* A failed catalogue is not cacheable: the next view to ask should retry
           rather than inherit this rejection forever. */
        state.catalogP = null;
        throw err;
      });
    }
    return state.catalogP;
  }

  function volumes(force) {
    if (force || !state.volumesP) {
      state.volumesP = api.volumes().then(null, function (err) {
        state.volumesP = null;
        throw err;
      });
    }
    return state.volumesP;
  }
  /*
   * Settings live here rather than in the Settings view, because four other views read
   * them: Overview needs `size_on_disk` for its figures, Clean needs `preset`,
   * `min_age_hours`, `confirm_dangerous` and `exclusions`, Explorer will need
   * `volume_ids`. A view that wrote its own copy would be the one that goes stale.
   */
  function settings() { return state.settings; }
  function defaults() { return state.defaults; }

  function adopt(payload) {
    if (payload && payload.settings) { state.settings = payload.settings; }
    if (payload && payload.defaults) { state.defaults = payload.defaults; }
    return state.settings;
  }

  function loadSettings() {
    return api.settingsGet().then(function (payload) {
      adopt(payload);
      return state.settings;
    });
  }

  /*
   * Save, then tell everyone. `persisted: false` means the profile directory is not
   * writable -- real on a managed machine -- and the honest thing is to say the change
   * is in force but will not survive a restart, rather than showing a silent success.
   */
  function saveSettings(changes) {
    return api.settingsSet(changes).then(function (payload) {
      adopt(payload);
      if (payload && payload.persisted === false) {
        ui.toast(i18n.t('settings.not_persisted'), { kind: 'warn' });
      }
      notifySettings();
      return state.settings;
    });
  }

  function onSettings(fn) {
    if (typeof fn === 'function' && settingsListeners.indexOf(fn) === -1) {
      settingsListeners.push(fn);
    }
  }

  function notifySettings() {
    for (var i = 0; i < settingsListeners.length; i += 1) {
      try {
        settingsListeners[i](state.settings);
      } catch (err) {
        if (window.console) { console.error('settings listener failed', err); }
      }
    }
  }
  /* --- the one-job-at-a-time guard ------------------------------------------- */

  /*
   * The engine already refuses a second job: `submit_scan` raises and the bridge
   * answers `busy` (bridge.py). That refusal is the authority -- this is only here so
   * the UI does not have to *provoke* it. Overview auto-scans on entry and Clean scans
   * on a button; without this, opening Overview during a clean produces an error toast
   * about a thing the user did not do.
   *
   * `claimJob` hands back the release function rather than an id, because the release
   * has to happen in the watcher's completion handler and passing a closure is harder
   * to forget than remembering to call releaseJob(theRightToken).
   */
  function claimJob(kind) {
    if (state.job) { return null; }
    var token = { kind: kind, owner: state.current };
    state.job = token;
    return function release() {
      if (state.job === token) { state.job = null; }
    };
  }

  function busyWith() { return state.job ? state.job.kind : null; }

  /* --- navigation ------------------------------------------------------------ */

  function markNav(name) {
    for (var i = 0; i < dom.navItems.length; i += 1) {
      var b = dom.navItems[i];
      /* An EXTRA route matches no data-view, so every item deactivates and the topbar
         title is the only thing saying where you are. That is the intent: the screen
         is a detour off the Clean view, and it carries its own way back. */
      var on = b.getAttribute('data-view') === name;
      b.classList.toggle('is-active', on);
      /* aria-current, not aria-selected: these are buttons in a list, not tabs
         (see index.html's comment on why this is not a tablist). */
      if (on) { b.setAttribute('aria-current', 'page'); } else { b.removeAttribute('aria-current'); }
    }
  }

  /* The topbar strings are `data-i18n` keys, set here and translated by i18n.apply --
     so a language switch re-reads them without app.js re-writing anything. */
  function markTitle(name) {
    dom.title.setAttribute('data-i18n', 'nav.' + name);
    dom.sub.setAttribute('data-i18n', 'nav.' + name + '.sub');
    i18n.apply(dom.title.parentNode);
  }
  /*
   * Switch views. Six things in a fixed order, and the order is the whole content of
   * this function:
   *
   *   1. leave() the outgoing view          -- so its poll timer stops before its DOM
   *                                            goes away, not after
   *   2. hide the outgoing section, clear the topbar actions
   *   3. mark the sidebar and the title
   *   4. show the incoming section
   *   5. mount() it if this is its first visit
   *   6. enter() it, then relang() if it was last drawn in the other language
   *
   * A throw from mount() or enter() shows an error inside that view's own section and
   * leaves the rest of the app alive: one broken view must not take the window down,
   * because six of the seven still work.
   */
  function go(name) {
    if (ROUTES.indexOf(name) === -1) { name = FIRST; }
    if (name === state.current) { return; }

    var outName = state.current;
    if (outName) {
      var out = views[outName];
      if (out && typeof out.leave === 'function') {
        try {
          out.leave();
        } catch (err) {
          if (window.console) { console.error('leave() failed for ' + outName, err); }
        }
      }
      dom.sections[outName].hidden = true;
    }
    ui.clear(dom.actions);

    state.current = name;
    markNav(name);
    markTitle(name);

    var host = dom.sections[name];
    host.hidden = false;
    var view = views[name];
    if (!view) {
      /* A view file that failed to load or register. Says which one, in the window,
         rather than leaving an empty panel. */
      ui.clear(host);
      host.appendChild(ui.emptyState({
        icon: 'icon-info', i18n: 'error.view_missing', text: name
      }));
      return;
    }
    render(name, view, host);
  }
  function render(name, view, host) {
    try {
      if (!state.mounted[name]) {
        ui.clear(host);
        view.mount(host);
        /* Set only after mount() returns: a half-built view should be rebuilt on the
           next visit, not treated as done. */
        state.mounted[name] = true;
      }
      if (typeof view.enter === 'function') { view.enter(host, dom.actions); }
      /*
       * The stale-language case. A view mounted in Vietnamese, hidden, then the user
       * switches to English: i18n's listener re-langs only what is on screen, because
       * re-rendering six hidden views on every switch is work nobody sees. So the
       * language each view was drawn in is remembered, and checked on the way back in.
       */
      if (state.langAt[name] && state.langAt[name] !== i18n.lang
          && typeof view.relang === 'function') {
        view.relang();
      }
      state.langAt[name] = i18n.lang;
    } catch (err) {
      if (window.console) { console.error('view ' + name + ' failed', err); }
      ui.clear(host);
      host.appendChild(ui.emptyState({
        icon: 'icon-caution',
        i18n: 'error.view_failed',
        text: err && err.text ? err.text() : String(err && err.message ? err.message : err)
      }));
      /* Let the next visit try again rather than pinning the failure. */
      state.mounted[name] = false;
    }
  }

  function relangCurrent() {
    var name = state.current;
    if (!name) { return; }
    var view = views[name];
    if (view && typeof view.relang === 'function') {
      try {
        view.relang();
      } catch (err) {
        if (window.console) { console.error('relang() failed for ' + name, err); }
      }
    }
    state.langAt[name] = i18n.lang;
  }
  /* --- language -------------------------------------------------------------- */

  function markLang(lang) {
    for (var i = 0; i < dom.langBtns.length; i += 1) {
      var b = dom.langBtns[i];
      var on = b.getAttribute('data-lang') === lang;
      b.classList.toggle('is-active', on);
      /* aria-pressed rather than aria-current: this is a two-state toggle pair, and a
         screen reader should say "VI, pressed" (docs/02-SPEC.md 7.4). */
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  /*
   * Switch, then persist. In that order, deliberately: the switch is instant and local,
   * writing settings.json is neither, and a user who clicks EN should see English
   * before the disk has been touched. A failed write is reported by saveSettings and
   * costs nothing but the preference not surviving a restart.
   */
  function pickLang(next) {
    if (next === i18n.lang) { return; }
    i18n.setLang(next);
    saveSettings({ language: next }).then(null, ui.showError);
  }

  /* --- administrator --------------------------------------------------------- */

  function loadAdmin() {
    return api.adminState().then(function (a) {
      state.admin = a;
      markAdmin();
      return a;
    }, function (err) {
      /* An unknown elevation state shows as neither banner. Claiming "no admin" when
         the call failed would offer a UAC relaunch this process may not need. */
      if (window.console) { console.warn('admin_state failed', err); }
      return null;
    });
  }

  function markAdmin() {
    var a = state.admin;
    if (!a) { return; }
    dom.adminOk.hidden = !a.admin;
    dom.adminBanner.hidden = !!a.admin;
    if (!a.admin) {
      /* The count is the point of the banner: "16 rows are locked" is a reason to
         press the button, "no admin" is not. */
      dom.adminCount.textContent = i18n.tn('admin.blocked', a.blocked_count || 0);
      dom.adminRelaunch.disabled = !a.can_relaunch;
    }
  }
  /*
   * The UAC round trip. Three outcomes, and all three are normal:
   *
   *   launched        -- the elevated process is starting and window.py is closing this
   *                      window. Nothing to update; the toast is the last thing shown.
   *   already_admin   -- somebody clicked a stale banner. Re-read the state instead of
   *                      arguing with it.
   *   declined        -- the user said no at the prompt. Not an error: the app keeps
   *                      running with the admin rows disabled, which is the design.
   */
  function relaunch() {
    dom.adminRelaunch.disabled = true;
    api.adminRelaunch().then(function (r) {
      if (r && r.launched) {
        ui.toast(i18n.t('admin.relaunching'), { kind: 'info' });
        return;
      }
      dom.adminRelaunch.disabled = false;
      if (r && r.already_admin) { loadAdmin(); return; }
      ui.toast(i18n.t('admin.declined'), { kind: 'warn' });
    }, function (err) {
      dom.adminRelaunch.disabled = false;
      ui.showError(err);
    });
  }

  /* --- boot ------------------------------------------------------------------- */

  function wire() {
    var i;
    for (i = 0; i < dom.navItems.length; i += 1) {
      dom.navItems[i].addEventListener('click', function () {
        go(this.getAttribute('data-view'));
      });
    }
    for (i = 0; i < dom.langBtns.length; i += 1) {
      dom.langBtns[i].addEventListener('click', function () {
        pickLang(this.getAttribute('data-lang'));
      });
    }
    dom.adminRelaunch.addEventListener('click', relaunch);

    /* One listener for the whole app: i18n has already re-applied every [data-i18n] in
       the document by the time this runs, so what is left is the chrome this file
       writes itself and whatever the visible view drew from engine data. */
    i18n.onChange(function (lang) {
      markLang(lang);
      markAdmin();
      relangCurrent();
    });
  }
  /*
   * What happens once the bridge answers, in order and with the failure of each step
   * decided on purpose:
   *
   *   settings   -- if unreadable, say so and carry on in Vietnamese. A missing
   *                 settings file is the first-run case and must not block the app.
   *   language   -- applied before the first view is drawn, so nothing renders twice.
   *   Overview   -- opened last, because it scans, and it should scan against the
   *                 settings that are actually in force.
   *   admin      -- not awaited. The sidebar banner appearing 80 ms late is invisible;
   *                 delaying the first paint on it would not be.
   */
  function checkAppUpdate() {
    api.updaterCheck(false).then(function (res) {
      if (res && res.available) {
        if (dom.settingsUpdateBadge) {
          dom.settingsUpdateBadge.hidden = false;
        }
        ui.toast(i18n.t('update.toast_available'), { kind: 'info' });
      }
    }, function () {
      /* Silent on startup if offline */
    });
  }

  function afterBridge() {
    loadSettings().then(null, function (err) {
      ui.showError(err);
    }).then(function () {
      if (state.settings && state.settings.language) {
        i18n.setLang(state.settings.language);
      }
      markLang(i18n.lang);
      go(FIRST);
      loadAdmin();
      checkAppUpdate();
    });
  }

  function boot() {
    cacheDom();
    wire();
    markNav(FIRST);
    markTitle(FIRST);
    markLang(i18n.lang);
    i18n.apply(document);

    /* Something in the panel while the bridge starts. It is normally on screen for one
       or two frames; it exists so that the ten-second failure case is a stated wait
       rather than an empty window. go() clears it. */
    var host = dom.sections[FIRST];
    host.hidden = false;
    host.appendChild(ui.emptyState({ icon: 'icon-refresh', i18n: 'boot.waiting' }));

    api.ready().then(afterBridge, function (err) {
      /* No bridge means no engine: every view would fail identically, so this is the
         one modal in the app that cannot be dismissed. */
      ui.fatal(err && err.text ? err.text() : String(err && err.message ? err.message : err));
    });
  }
  /*
   * The shared surface. Everything a view is allowed to assume about the rest of the
   * app is on this object -- if it is not here, the view owns it.
   */
  ADC.app = {
    views: ORDER.slice(),
    routes: ROUTES.slice(),
    go: go,
    current: function () { return state.current; },

    settings: settings,
    defaults: defaults,
    saveSettings: saveSettings,
    onSettings: onSettings,

    catalog: catalog,
    volumes: volumes,

    admin: function () { return state.admin; },
    refreshAdmin: loadAdmin,

    claimJob: claimJob,
    busyWith: busyWith
  };

  /* Last line of the last script. The tag sits at the end of <body>, so every element
     cacheDom() wants is already parsed -- the readyState check is for the day somebody
     moves the tag into <head>. */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
