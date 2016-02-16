openerp.web_celery = function(instance){
    var pending_jobs = 0;
    var _t = openerp._t;

    // That's 20 minutes!  This should account for the time in the queue plus
    // the running time.
    var JOB_TIME_LIMIT = 1200000;

    var get_progress_channel = function(job) {
        return 'celeryapp:' + job.uuid + ':progress';
    };

    var isOk = function(v) {return _.isNumber(v) && !_.isNaN(v);}

    openerp.JobThrobber = openerp.Widget.extend({

        init: function(parent, options) {
            this._super(parent);
            var uuid, bus;
            this.percent = 0;
            this.done = $.Deferred();
            this.uuid = uuid = options.params.uuid;
            this.next_action = options.params.next_action;
            this.title = _t('Working');
            this.message = _t('Your request is being processed (or about '+
                              'to be processed.)  Please wait.');
            bus = this.bus = new openerp.bus.Bus();
            bus.add_channel(get_progress_channel(options.params));
            bus.on('notification', this, this.on_job_notification);
            pending_jobs += 1;
            this.show().done(_.bind(this.start_waiting, this));
        },


        update: function(progress, valuemin, valuemax, message) {
            if (isOk(progress))
                if (!this.progress || this.progress < progress)
                    this.progress = progress;
            // Once set, the valuemin and valuemax cannot be updated.
            if (isOk(valuemin) && !isOk(this.valuemin))
                this.valuemin = valuemin;
            if (isOk(valuemax) && !isOk(this.valuemax))
                this.valuemax = valuemax;
            if (isOk(this.progress) && isOk(this.valuemax) && isOk(this.valuemin))
                try {
                    var p = this.percent = Math.round(this.progress/(this.valuemax-this.valuemin)*100);
                    if (p < 0 || p > 100)
                        throw ('AssertionError: percent makes no sense');
                } catch (error) {
                    // Safely avoid any non-sensible value
                    this.progress = this.valuemin = this.valuemax = null;
                    this.percent = 0;
                }
            this.message = message;
            this.updateView();
        },


        on_job_notification: function(params){
            var channel = params[0];
            var message = params[1];
            var status = message.status;
            if (status && (status == 'success' || status == 'failure')) {
                if (status != 'failure') {
                    var next = message.result;
                    if (next) {
                        this.next_action = next;
                    }
                }
                else {
                    _.delay(_.bind(this.show_failure, this, message), 1);
                }
                this.stop_waiting();
            } else {
                this.update(message.progress, message.valuemin,
                            message.valuemax, message.message);
            }
        },

        start_waiting: function() {
            this.bus.start_polling();
            var timer = $.elapsed(JOB_TIME_LIMIT);
            var done = this.done;
            var self = this;
            $.whichever(done, timer).done(function(which){
                if (which == timer) {
                    self.do_warn(
                        _t('Ask for help'),
                        _t("You're stuck waiting for a job that is taking "+
                           "forever.  Grab the next IT guy and ask help."),
                        true
                    );
                    self.stop_waiting();
                }
            });
        },

        stop_waiting: function() {
            this.bus.stop_polling();
            if (!this.isDestroyed()) {
                var parent = this.getParent();
                var self = this;
                if (!!self.next_action) {
                    _.delay(function(){
                        // First go back to remove the progress bar level from
                        // the breadcumbs, and then go the specified action.
                        parent.do_action('history_back').then(
                            function(){parent.do_action(self.next_action);}
                        );
                    }, 1);
                } else {
                    _.delay(function(){parent.do_action('history_back');}, 1);
                }
                this.destroy();
            }
            this.done.resolve();
        },

        destroy: function() {
            pending_jobs -= 1;
            this._super();
        }
    });


    openerp.ProgressBarThrobber = openerp.JobThrobber.extend({
        template: "BackgroundJobProgress",

        show: function() {
            return this.appendTo($("body"));
        },

        start: function() {
            var res = $.Deferred();
            this.$el.on('show.bs.modal', function(){
                res.resolve();
            });
            this.$el.modal('show');
            return res.promise();
        },

        updateView: function() {
            if (this.message) {
                this.$('.message').text(this.message);
            }
            var $progressbar = this.$('.progress-bar');
            if (isOk(this.valuemin) && !$progressbar.attr('aria-valuemin')) {
                $progressbar.attr('aria-valuemin', this.valuemin);
            }
            if (isOk(this.valuemax) && !$progressbar.attr('aria-valuemax')) {
                $progressbar.attr('aria-valuemax', this.valuemax);
            }
            if (isOk(this.progress)) {
                $progressbar.attr('aria-valuenow', this.progress);
            }
            // percent should be always ok, but will show only a progress bar
            // if there's a progress value.
            if (isOk(this.percent) && isOk(this.progress)) {
                $progressbar.attr('style', 'width: ' + this.percent + '%');
                var $pmsg = $progressbar.find('.percent-message');
                if ($pmsg.length){
                    $pmsg.text(this.percent + '%');
                } else {
                    $msg = $progressbar.find('.progress-bar');
                    $msg.add('<span aria-hidden="true" class="percent-message">' +
                             this.percent + '%</span>');
                }
            }
        },
        show_failure: function(message) {
            var cm = new instance.web.CrashManager();
            cm.show_error({
                type: _t("Server Error"),
                message: message.message,
                data: {debug: message.traceback}
            });
        },

        destroy: function() {
            // For some reason, when showing the dialog two backdrops are
            // being placed in the DOM, but hiding only removes one: remove
            // them all.
            this.$el.on('hide.bs.modal', function() {
                $('body .modal-backdrop.in').detach();
            });
            this.$el.modal('hide');
            this._super();
        }
    });

    if (!!instance){
        if(!!instance.hasOwnProperty('web')){
            instance.web.client_actions.add('wait_for_background_job',
                                            'openerp.ProgressBarThrobber');
        }
    }
};

// Local Variables:
// indent-tabs-mode: nil
// End:
