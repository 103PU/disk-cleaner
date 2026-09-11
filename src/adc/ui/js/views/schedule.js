/*
 * views/schedule.js -- Scheduling, a P5 feature (docs/03-PLAN.md) with no engine behind
 * it yet: nothing in bridge.js registers a Windows scheduled task, so a form here could
 * be filled in and would save nowhere. The honest placeholder is the requirement -- a
 * schedule the user believes exists and that never runs is worse than no schedule.
 */
(function () {
  'use strict';

  var ADC = window.ADC = window.ADC || {};
  var views = ADC.views = ADC.views || {};
  var ui = ADC.ui;

  /*
   * mount is the whole contract here. There is no enter() because the panel holds no
   * engine data to refresh, no leave() because it starts no poll, and no relang()
   * because every string below is a dictionary key -- ADC.i18n.apply() re-reads the
   * data-i18n attributes ui.comingSoon wrote, so a language switch needs nothing from
   * this file. That is precisely why it must be built from keys and not from text.
   */
  views.schedule = {
    mount: function (host) {
      host.appendChild(ui.comingSoon({
        i18n: 'schedule.soon.title',
        body: 'schedule.soon.body',
        items: [
          'schedule.soon.1',
          'schedule.soon.2',
          'schedule.soon.3',
          'schedule.soon.4',
          'schedule.soon.5'
        ]
      }));
    }
  };
})();
