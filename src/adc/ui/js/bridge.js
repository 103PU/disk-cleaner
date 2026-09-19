/*
 * bridge.js -- ADC.api. The only door out of this page.
 *
 * pywebview hands the Python Bridge object to the renderer as window.pywebview.api,
 * so a call here is an in-process call: no HTTP, no port, no origin, nothing on the
 * network to find (docs/02-SPEC.md 8, SEC-01). This file adds three things on top of
 * that raw surface and nothing else:
 *
 *   1. It unwraps the envelope. Every bridge method answers {ok: true, data} or
 *      {ok: false, error: {code, message_vi, message_en}} and NEVER raises -- see
 *      `guarded` in src/adc/shell/bridge.py. So the natural JS shape is a promise that
 *      resolves with `data` and rejects with an ApiError carrying `code`, and views
 *      get to write try/catch instead of checking `.ok` at 60 call sites.
 *   2. It waits for the bridge. The document paints before pywebview has injected the
 *      api object, so every call goes through `ready`.
 *   3. It polls jobs. Scan and clean are both "start, then poll job_poll until done",
 *      the cursor arithmetic is fiddly, and getting it wrong duplicates log lines --
 *      so it is written once here as `watch()` rather than twice in two views.
 *
 * What this file deliberately does NOT do: send a path. Not one method below takes a
 * filesystem path, because not one method on the Python side accepts one. The page
 * sends ids; the catalogue decides what an id means on this machine. `reveal` takes a
 * target_id and answers with the folder it opened -- paths may leave, they may not
 * enter, and that asymmetry is the fix for SEC-02.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};

  /* How long to wait for `pywebviewready` before declaring the shell broken. The
     event normally fires within a frame or two of the document being parsed; ten
     seconds is "the Python side died during startup", which is a different problem
     from "it is slow" and deserves the fatal dialog rather than a spinner forever. */
  var READY_TIMEOUT_MS = 10000;

  /* job_poll cadence. 250 ms is docs/02-SPEC.md 3.1's figure: fast enough that a
     progress bar looks continuous, slow enough that a walk over 70 000 files is not
     competing with the renderer for the GIL. */
  var POLL_MS = 250;
  /*
   * A refusal from the engine, as an Error so it survives a throw.
   *
   * `code` is the thing to branch on -- 'plan_expired' means re-preview, 'busy' means
   * a job is already running, 'needs_admin' means show the relaunch button -- and it
   * is a stable engine-side identifier. The two message strings are already localised
   * by the bridge, which is why there is no code-to-text table in this UI: the engine
   * logs the same wording it shows, so the two cannot drift.
   */
  function ApiError(code, vi, en) {
    this.name = 'ApiError';
    this.code = code || 'unknown';
    this.message_vi = vi || '';
    this.message_en = en || '';
    /* Error.message is the English one, because that is what lands in console traces
       and in a bug report; the UI shows text() instead. */
    this.message = en || vi || this.code;
    this.stack = new Error(this.message).stack;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;
  ApiError.prototype.text = function () {
    return ADC.i18n ? ADC.i18n.pickField(this, 'message') : this.message;
  };

  /* --- readiness ------------------------------------------------------------- */

  var readyPromise = null;

  function raw() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  function ready() {
    if (readyPromise) { return readyPromise; }
    readyPromise = new Promise(function (resolve, reject) {
      if (raw()) { resolve(raw()); return; }
      var timer = window.setTimeout(function () {
        reject(new ApiError(
          'no_bridge',
          'Cầu nối tới engine không sẵn sàng.',
          'The Python bridge never became available.'
        ));
      }, READY_TIMEOUT_MS);
      window.addEventListener('pywebviewready', function () {
        window.clearTimeout(timer);
        /* The event can, in principle, fire before the attribute is assigned. */
        if (raw()) {
          resolve(raw());
        } else {
          reject(new ApiError('no_bridge', 'Cầu nối tới engine không sẵn sàng.',
            'pywebviewready fired but window.pywebview.api is absent.'));
        }
      }, { once: true });
    });
    return readyPromise;
  }
  /* --- calling --------------------------------------------------------------- */

  function unwrap(name, reply) {
    /* A reply that is not an envelope means the Python side changed shape under us --
       a real possibility during development, and one worth naming rather than
       throwing a TypeError from a property access three frames later. */
    if (!reply || typeof reply !== 'object' || typeof reply.ok !== 'boolean') {
      throw new ApiError('bad_envelope',
        'Engine trả về dữ liệu không đúng dạng.',
        'Bridge method ' + name + ' returned a non-envelope reply.');
    }
    if (reply.ok) { return reply.data; }
    var err = reply.error || {};
    throw new ApiError(err.code, err.message_vi, err.message_en);
  }

  /*
   * One call. Every named method below is one line on top of this.
   *
   * The catch converts a rejected pywebview promise into an ApiError too. It should
   * never fire -- `guarded` means the Python side answers rather than raises -- but
   * when pywebview does reject, its value is {isError: true, value: {message, name,
   * stack}} and that `stack` is a full Python traceback with absolute paths
   * (webview/util.py:234-250). Reading `.message` off it and dropping the rest keeps
   * the traceback out of the page, which is the same thing `guarded` is for.
   */
  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return ready().then(function (api) {
      var fn = api[name];
      if (typeof fn !== 'function') {
        throw new ApiError('no_method',
          'Chức năng này không có trong engine.',
          'The bridge has no method named ' + name + '.');
      }
      return fn.apply(api, args).then(
        function (reply) { return unwrap(name, reply); },
        function (raised) {
          if (raised instanceof ApiError) { throw raised; }
          var detail = raised && raised.value ? raised.value : raised;
          var msg = (detail && detail.message) ? String(detail.message) : String(raised);
          throw new ApiError('internal',
            'Lỗi nội bộ khi gọi engine. Chi tiết đã ghi vào log.',
            'Bridge call ' + name + ' rejected: ' + msg);
        }
      );
    });
  }
  /* --- job polling ----------------------------------------------------------- */

  /*
   * Poll one job to completion. Used by both the scan and the clean, because from here
   * they are the same shape: something started on a worker thread, and this ticks
   * until `done`.
   *
   * `since_event` is a cursor, not a page number. Pass back the `next_event` the
   * previous reply gave and each log line arrives exactly once; get it wrong and the
   * console repeats itself, which is how v1's pane behaved. The cursor is held here so
   * no view has to hold it.
   *
   * Returns {promise, cancel}. `cancel()` asks the engine to stop (job_cancel) AND
   * stops polling locally, so a user who closes a view mid-scan does not leave a timer
   * running against a job nobody is watching. The promise resolves with the final
   * snapshot even when the job failed or was cancelled -- those are outcomes the
   * summary view has to render, not exceptions. It rejects only when polling itself
   * became impossible.
   */
  function watch(jobId, handlers) {
    var h = handlers || {};
    var since = 0;
    var stopped = false;
    var timer = null;
    var settle = {};
    var promise = new Promise(function (resolve, reject) {
      settle.resolve = resolve;
      settle.reject = reject;
    });

    function tick() {
      if (stopped) { return; }
      call('job_poll', jobId, since).then(function (snap) {
        if (stopped) { return; }
        var events = snap.events || [];
        since = typeof snap.next_event === 'number' ? snap.next_event : since + events.length;
        if (events.length && h.onEvents) { h.onEvents(events, snap); }
        if (h.onSnapshot) { h.onSnapshot(snap); }
        if (snap.done) {
          stopped = true;
          settle.resolve(snap);
          return;
        }
        timer = window.setTimeout(tick, POLL_MS);
      }, function (err) {
        if (stopped) { return; }
        stopped = true;
        settle.reject(err);
      });
    }
    tick();

    return {
      promise: promise,
      /* Two separate stops, and both are needed. `stopPolling` alone would leave the
         engine walking a million files for nobody; `job_cancel` alone would leave this
         timer ticking against a job that is winding down. */
      cancel: function () {
        if (stopped) { return Promise.resolve(false); }
        return call('job_cancel', jobId).then(function () { return true; },
          function () { return false; });
      },
      stopPolling: function () {
        stopped = true;
        if (timer !== null) { window.clearTimeout(timer); timer = null; }
      }
    };
  }

  /* --- the surface ----------------------------------------------------------- */

  /*
   * One wrapper per Python method, camelCase here and snake_case there. Twenty-six,
   * which is all of them -- a method missing from this list is a method the UI cannot
   * reach, and that is exactly how the History view and the "Mở file log" button were
   * found to be unwired. tests/test_bridge.py checks the two lists against each other
   * so a new Python method cannot stay unreachable quietly.
   *
   * Argument coercion is not repeated here. Every Python method takes `object` and
   * coerces defensively on its own side, because a page is untrusted input even when
   * we wrote the page; duplicating those checks in JS would give two answers to the
   * same question.
   */
  ADC.api = {
    ApiError: ApiError,
    ready: ready,
    call: call,
    watch: watch,
    pollInterval: POLL_MS,

    catalog: function () { return call('catalog'); },
    volumes: function () { return call('volumes'); },
    adminState: function () { return call('admin_state'); },
    adminRelaunch: function () { return call('admin_relaunch'); },

    settingsGet: function () { return call('settings_get'); },
    settingsSet: function (changes) { return call('settings_set', changes); },
    scanStart: function (volumeIds, targetIds) {
      return call('scan_start', volumeIds || null, targetIds || null);
    },
    jobPoll: function (jobId, since) { return call('job_poll', jobId, since || 0); },
    jobCancel: function (jobId) { return call('job_cancel', jobId); },

    /* The Disk Explorer's two calls. Both take a handle the engine minted -- a volume
       letter to start a chain, or a node_id from a level already on screen to drill
       down or climb back up. Neither can be given a path, which is why drilling is
       safe: the page can only ask for a folder it was already shown. */
    exploreStart: function (volumeId, nodeId) {
      return call('explore_start', volumeId || null, nodeId || null);
    },
    exploreReveal: function (nodeId) { return call('explore_reveal', nodeId); },

    /* The two halves of a clean, and they are separate on purpose: cleanPlan is a dry
       run that mints a token, cleanExecute redeems it. Nothing about *what* gets
       deleted travels with the second call, so a page cannot widen a clean after the
       user has approved the preview. */
    cleanPlan: function (selection, allowDangerous) {
      return call('clean_plan', selection, allowDangerous === true);
    },
    cleanExecute: function (planToken) { return call('clean_execute', planToken); },

    /* The Project Sweeper. `sweepDefaults` fills the two controls before anything
       runs; `sweepStart` is the one call in this file that sends a path, and it does
       so because SPEC 6.3 has the user choose the folder their projects live in and
       there is no handle for a folder the engine has never shown them. Everything
       after it is handles again: `sweepPlan` takes the job id whose table the ticks
       came from plus those rows' node_ids, and `sweepExecute` takes nothing but the
       token `sweepPlan` minted.

       `minAgeDays` is checked with typeof rather than `||`, because 0 is a real
       answer -- "no age filter" -- and `0 || null` would silently turn it into the
       thirty-day default. */
    pickFolder: function (initial) { return call('pick_folder', initial || null); },
    sweepDefaults: function () { return call('sweep_defaults'); },
    sweepStart: function (root, minAgeDays) {
      return call('sweep_start', root || null,
        typeof minAgeDays === 'number' ? minAgeDays : null);
    },
    sweepPlan: function (jobId, nodeIds) { return call('sweep_plan', jobId, nodeIds || []); },
    sweepExecute: function (planToken) { return call('sweep_execute', planToken); },
    sweepReveal: function (nodeId) { return call('sweep_reveal', nodeId); },

    /*
     * Shadow copies: one read and a second handshake of exactly the same shape as the
     * clean's. A preview mints a receipt, `vssApply` redeems it -- and the phrase is
     * sent with the redemption rather than checked here, because the page is not
     * where a confirmation for `vssadmin delete shadows /all` can be enforced.
     */
    vssStatus: function () { return call('vss_status'); },
    vssResizePreview: function (volumeId, limit) {
      return call('vss_resize_preview', volumeId || null, limit || null);
    },
    vssDeletePreview: function (volumeId, scope) {
      return call('vss_delete_preview', volumeId || null, scope || null);
    },
    vssApply: function (actionToken, phrase) {
      return call('vss_apply', actionToken, phrase || null);
    },

    reveal: function (targetId) { return call('reveal', targetId); },
    history: function (limit) { return call('history', limit); },
    reportDetail: function (jobId) { return call('report_detail', jobId); },
    openLog: function () { return call('open_log'); },

    /* Automated maintenance schedule (Task Scheduler) */
    scheduleGet: function () { return call('schedule_get'); },
    scheduleSet: function (config) { return call('schedule_set', config || {}); },
    scheduleDelete: function () { return call('schedule_delete'); },
    scheduleRunNow: function () { return call('schedule_run_now'); },

    /* In-app updater */
    updaterCheck: function (force) { return call('updater_check', force === true); },
    updaterDownloadStart: function () { return call('updater_download_start'); },
    updaterDownloadProgress: function () { return call('updater_download_progress'); },
    updaterDownloadCancel: function () { return call('updater_download_cancel'); },
    updaterInstall: function () { return call('updater_install'); }
  };
})();
