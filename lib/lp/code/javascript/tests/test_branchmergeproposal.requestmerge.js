/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Tests for lp.code.branchmergeproposal.requestmerge.
 */

YUI.add(
  "lp.code.branchmergeproposal.requestmerge.test",
  (Y) => {
    const module = Y.lp.code.branchmergeproposal.requestmerge;
    const tests = Y.namespace("lp.code.branchmergeproposal.requestmerge.test");
    const SELF_LINK =
      "https://code.launchpad.test/api/devel/~foo/repo/+merge/1";

    const TestMixin = {
      setUp() {
        this.fixture = Y.one("#fixture");
        this.fixture.append(
          Y.Node.create(Y.one("#requestmerge-template").getContent()),
        );
        this.mockio = new Y.lp.testing.mockio.MockIo();
        this.lp_client = new Y.lp.client.Launchpad({
          io_provider: this.mockio,
        });
        this.poll_callbacks = [];
        window.LP = {
          cache: {
            merge_request_enabled: true,
            context: {
              self_link: SELF_LINK,
              queue_status: "Work in progress",
              date_merge_requested: null,
            },
          },
        };
      },

      tearDown() {
        this.fixture.empty();
        delete this.lp_client;
        delete this.mockio;
        delete window.LP;
        delete this.poll_callbacks;
      },

      setup() {
        const test = this;
        module.setup({
          lp_client: this.lp_client,
          schedule_poll(callback) {
            test.poll_callbacks.push(callback);
          },
        });
      },

      makeMergeAvailable(callback) {
        this.setup();
        Y.Assert.areEqual(
          "ws.op=getMergeCriteria",
          this.mockio.last_request.config.data,
        );
        const link = Y.one(".menu-link-request_merge");
        Y.Assert.isTrue(link.hasClass(module.CLASSES.loading));
        Y.Assert.isFalse(link.hasClass(module.CLASSES.mergeProposal));
        Y.Assert.isFalse(link.hasClass(module.CLASSES.sprite));
        Y.Assert.areEqual("Checking merge...", link.get("text"));
        Y.Assert.isNotNull(link.one(".request-merge-spinner"));
        this.respondJSON({ can_be_merged: true, criteria: {} });
        this.wait(() => {
          Y.Assert.isFalse(link.hasClass(module.CLASSES.loading));
          Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
          Y.Assert.isTrue(link.hasClass(module.CLASSES.sprite));
          Y.Assert.areEqual("Request merge", link.get("text"));
          callback();
        }, 10);
      },

      clickLink() {
        Y.one(".menu-link-request_merge").simulate("click");
      },

      respondJSON(value) {
        this.mockio.success({
          responseText: Y.JSON.stringify(value),
          responseHeaders: { "Content-Type": "application/json" },
        });
      },

      runPoll() {
        Y.Assert.areEqual(1, this.poll_callbacks.length);
        this.poll_callbacks.shift()();
      },

      assertInProgress() {
        const link = Y.one(".menu-link-request_merge");
        Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
        Y.Assert.isTrue(link.hasClass(module.CLASSES.inProgress));
        Y.Assert.isTrue(link.hasClass(module.CLASSES.noLink));
        Y.Assert.isFalse(link.hasClass(module.CLASSES.jsAction));
        Y.Assert.isFalse(link.hasClass(module.CLASSES.mergeProposal));
        Y.Assert.isFalse(link.hasClass(module.CLASSES.sprite));
        Y.Assert.areEqual("Merge in progress", link.get("text"));
        Y.Assert.isNotNull(link.one(".request-merge-spinner"));
        Y.Assert.areEqual("", link.getAttribute("href"));
        Y.Assert.areEqual("true", link.getAttribute("aria-disabled"));
        Y.Assert.areEqual(
          module.IN_PROGRESS_MESSAGE,
          link.getAttribute("title"),
        );
      },
    };

    tests.suite = new Y.Test.Suite("BranchMergeProposal Request Merge Tests");
    tests.suite.add(
      new Y.Test.Case(
        Y.merge(TestMixin, {
          name: "request-merge-tests",

          /*
           * CHECKING and READY: the criteria check enables the link and a
           * click posts requestMerge.
           */

          test_click_posts_request_merge() {
            this.makeMergeAvailable(() => {
              this.clickLink();

              Y.Assert.areEqual(
                "/api/devel/~foo/repo/+merge/1",
                this.mockio.last_request.url,
              );
              Y.Assert.areEqual(
                "ws.op=requestMerge",
                this.mockio.last_request.config.data,
              );
              Y.Assert.areEqual("POST", this.mockio.last_request.config.method);
              const link = Y.one(".menu-link-request_merge");
              Y.Assert.isTrue(link.hasClass(module.CLASSES.loading));
              Y.Assert.isFalse(link.hasClass(module.CLASSES.mergeProposal));
              Y.Assert.isFalse(link.hasClass(module.CLASSES.sprite));
              Y.Assert.areEqual("Requesting merge...", link.get("text"));
              Y.Assert.isNotNull(link.one(".request-merge-spinner"));
            });
          },

          test_refresh_does_nothing_while_request_post_is_pending() {
            this.makeMergeAvailable(() => {
              this.clickLink();
              const requestCount = this.mockio.requests.length;

              module.refresh();
              this.clickLink();

              Y.Assert.areEqual(requestCount, this.mockio.requests.length);
              Y.Assert.isTrue(
                Y.one(".menu-link-request_merge").hasClass(
                  module.CLASSES.loading,
                ),
              );
              this.respondJSON("Merge successfully queued");
            });
          },

          test_refresh_response_does_not_restore_busy_action() {
            this.makeMergeAvailable(() => {
              module.refresh();
              const permissionRequest = this.mockio.last_request;
              this.clickLink();
              const mergeRequest = this.mockio.last_request;

              permissionRequest.successJSON(true);
              this.wait(() => {
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.areEqual(3, this.mockio.requests.length);
                Y.Assert.isTrue(link.hasClass(module.CLASSES.loading));
                Y.Assert.areEqual("Requesting merge...", link.get("text"));

                this.clickLink();
                Y.Assert.areEqual(3, this.mockio.requests.length);
                mergeRequest.successJSON("Merge successfully queued");
              }, 10);
            });
          },

          test_success_shows_feedback_and_in_progress_action() {
            this.makeMergeAvailable(() => {
              this.clickLink();
              this.respondJSON("Merge successfully queued");

              this.wait(() => {
                const notification = Y.one(
                  "#request-notifications .informational.message",
                );
                Y.Assert.isNotNull(notification);
                Y.Assert.areEqual(
                  "Merge successfully queued",
                  notification.getHTML(),
                );
                this.assertInProgress();
                Y.Assert.areEqual(1, this.poll_callbacks.length);
              }, 10);
            });
          },

          test_failure_shows_feedback_and_restores_link() {
            this.makeMergeAvailable(() => {
              this.clickLink();
              this.mockio.failure({
                status: 400,
                responseText: "Merge proposal cannot be merged.",
              });

              this.wait(() => {
                const notification = Y.one(
                  "#request-notifications .error.message",
                );
                Y.Assert.isNotNull(notification);
                Y.Assert.areEqual(
                  "Merge proposal cannot be merged.",
                  notification.getHTML(),
                );
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
                Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
                Y.Assert.isTrue(link.hasClass(module.CLASSES.sprite));
                Y.Assert.areEqual("Request merge", link.get("text"));
                Y.Assert.isNull(link.one(".request-merge-spinner"));
              }, 10);
            });
          },

          test_request_failure_without_body_shows_default_message() {
            this.makeMergeAvailable(() => {
              this.clickLink();
              this.mockio.failure({ status: 500, responseText: "" });

              this.wait(() => {
                const notification = Y.one(
                  "#request-notifications .error.message",
                );
                Y.Assert.isNotNull(notification);
                Y.Assert.areEqual(
                  "Request to merge this proposal failed.",
                  notification.getHTML(),
                );
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
                Y.Assert.areEqual("Request merge", link.get("text"));
              }, 10);
            });
          },

          test_criteria_failure_still_enables_the_link() {
            // The merge endpoint enforces the criteria itself, so a
            // failed check must not disable the action.
            this.setup();
            Y.Assert.areEqual(
              "ws.op=getMergeCriteria",
              this.mockio.last_request.config.data,
            );
            this.mockio.failure({
              status: 503,
              responseText: "Service unavailable",
            });

            this.wait(() => {
              const link = Y.one(".menu-link-request_merge");
              Y.Assert.isFalse(link.hasClass(module.CLASSES.blocked));
              Y.Assert.isFalse(link.hasClass(module.CLASSES.loading));
              Y.Assert.isTrue(link.hasClass(module.CLASSES.jsAction));
              Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
              Y.Assert.areEqual("Request merge", link.get("text"));
              Y.Assert.isNull(Y.one(".request-merge-blocked-reason"));
              this.clickLink();
              Y.Assert.areEqual(
                "ws.op=requestMerge",
                this.mockio.last_request.config.data,
              );
            }, 10);
          },

          /*
           * BLOCKED: the link is disabled and the failed criteria are
           * listed below it.
           */

          test_blocked_merge_on_load_disables_link_quietly() {
            this.setup();
            this.respondJSON({
              can_be_merged: false,
              criteria: {
                is_approved: {
                  passed: false,
                  error: "Proposal has not been approved",
                },
              },
            });

            this.wait(() => {
              Y.Assert.isNull(Y.one("#request-notifications .warning.message"));
              const link = Y.one(".menu-link-request_merge");
              Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
              Y.Assert.isTrue(link.hasClass(module.CLASSES.blocked));
              Y.Assert.isTrue(link.hasClass(module.CLASSES.noLink));
              Y.Assert.isFalse(link.hasClass(module.CLASSES.jsAction));
              Y.Assert.areEqual("Merge blocked", link.get("text"));
              Y.Assert.areEqual("", link.getAttribute("href"));
              Y.Assert.areEqual("true", link.getAttribute("aria-disabled"));
              Y.Assert.areEqual(
                "Merge is currently blocked: Proposal has not been approved",
                link.getAttribute("title"),
              );
              const reason = Y.one(".request-merge-blocked-reason");
              Y.Assert.isNotNull(reason);
              Y.Assert.areEqual(
                "Proposal has not been approved",
                reason.get("text"),
              );
              this.clickLink();
              Y.Assert.areEqual(1, this.mockio.requests.length);
            }, 10);
          },

          test_blocked_merge_after_failed_request_shows_criteria() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              this.runPoll();
              this.respondJSON({ pending: false, merged: false });
              this.wait(() => {
                Y.Assert.areEqual(
                  "ws.op=getMergeCriteria",
                  this.mockio.last_request.config.data,
                );
                this.respondJSON({
                  can_be_merged: false,
                  criteria: {
                    is_approved: {
                      passed: false,
                      error: "Proposal has not been approved",
                    },
                    has_no_conflicts: {
                      passed: false,
                      error: "Merge conflicts are present",
                    },
                    CI_checks_passed: { passed: true },
                  },
                });
                this.wait(() => {
                  const notification = Y.one(
                    "#request-notifications .warning.message",
                  );
                  Y.Assert.isNotNull(notification);
                  Y.Assert.areEqual(
                    "Merge is currently blocked: " +
                      "Proposal has not been approved; " +
                      "Merge conflicts are present",
                    notification.getHTML(),
                  );
                  const link = Y.one(".menu-link-request_merge");
                  Y.Assert.isTrue(link.hasClass(module.CLASSES.blocked));
                  Y.Assert.areEqual(
                    "Proposal has not been approved; " +
                      "Merge conflicts are present",
                    Y.one(".request-merge-blocked-reason").get("text"),
                  );
                  Y.Assert.isNull(
                    Y.one(
                      "#request-notifications " + ".merge-in-progress-message",
                    ),
                  );
                }, 10);
              }, 10);
            }, 10);
          },

          /*
           * POLLING and MERGED: a pending request is polled until it is
           * merged or expires.
           */

          test_pending_request_shows_feedback_and_in_progress_action() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();

            Y.Assert.areEqual(
              "ws.op=getMergeRequestStatus",
              this.mockio.last_request.config.data,
            );
            this.respondJSON({ pending: true });

            this.wait(() => {
              const notification = Y.one(
                "#request-notifications .informational.message",
              );
              Y.Assert.isNotNull(notification);
              Y.Assert.areEqual(
                module.IN_PROGRESS_MESSAGE,
                notification.getHTML(),
              );
              this.assertInProgress();
              Y.Assert.areEqual(1, this.poll_callbacks.length);
              this.clickLink();
              Y.Assert.areEqual(1, this.mockio.requests.length);
            }, 10);
          },

          test_pending_request_is_polled_until_it_expires() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              this.runPoll();
              Y.Assert.areEqual(
                "ws.op=getMergeRequestStatus",
                this.mockio.last_request.config.data,
              );
              this.respondJSON({ pending: false, merged: false });
              this.wait(() => {
                Y.Assert.areEqual(
                  "ws.op=getMergeCriteria",
                  this.mockio.last_request.config.data,
                );
                this.respondJSON({ can_be_merged: true, criteria: {} });
                this.wait(() => {
                  const link = Y.one(".menu-link-request_merge");
                  Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
                  Y.Assert.isFalse(link.hasClass(module.CLASSES.inProgress));
                  Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
                  Y.Assert.areEqual("Request merge", link.get("text"));
                  Y.Assert.areEqual("#", link.getAttribute("href"));
                  Y.Assert.isNull(
                    Y.one(
                      "#request-notifications " + ".merge-in-progress-message",
                    ),
                  );
                  Y.Assert.areEqual(0, this.poll_callbacks.length);
                }, 10);
              }, 10);
            }, 10);
          },

          test_pending_request_is_polled_until_merged() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              this.runPoll();
              this.respondJSON({ pending: false, merged: true });
              this.wait(() => {
                const notification = Y.one(
                  "#request-notifications .informational.message",
                );
                Y.Assert.areEqual(
                  "The proposal has now been marked as merged.",
                  notification.getHTML(),
                );
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isTrue(link.hasClass(module.CLASSES.hidden));
                Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
                Y.Assert.isFalse(link.hasClass(module.CLASSES.inProgress));
                Y.Assert.areEqual(0, this.poll_callbacks.length);
              }, 10);
            }, 10);
          },

          test_merged_poll_prevents_later_refresh() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              this.runPoll();
              this.respondJSON({ pending: false, merged: true });
              this.wait(() => {
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isTrue(link.hasClass(module.CLASSES.hidden));

                module.refresh();

                Y.Assert.areEqual(2, this.mockio.requests.length);
                Y.Assert.isTrue(link.hasClass(module.CLASSES.hidden));
              }, 10);
            }, 10);
          },

          test_expired_request_checks_criteria_before_wiring_click() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: false });

            this.wait(() => {
              Y.Assert.areEqual(
                "ws.op=getMergeCriteria",
                this.mockio.last_request.config.data,
              );
              this.respondJSON({ can_be_merged: true, criteria: {} });
              this.wait(() => {
                Y.Assert.isNull(
                  Y.one("#request-notifications .informational.message"),
                );
                this.clickLink();
                Y.Assert.areEqual(3, this.mockio.requests.length);
                Y.Assert.areEqual(
                  "ws.op=requestMerge",
                  this.mockio.last_request.config.data,
                );
              }, 10);
            }, 10);
          },

          test_status_failure_on_load_checks_merge_criteria() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            Y.Assert.areEqual(
              "ws.op=getMergeRequestStatus",
              this.mockio.last_request.config.data,
            );
            this.mockio.failure({ status: 500, responseText: "Boom" });

            this.wait(() => {
              Y.Assert.areEqual(0, this.poll_callbacks.length);
              Y.Assert.areEqual(
                "ws.op=getMergeCriteria",
                this.mockio.last_request.config.data,
              );
              this.respondJSON({ can_be_merged: true, criteria: {} });
              this.wait(() => {
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isFalse(link.hasClass(module.CLASSES.inProgress));
                Y.Assert.isTrue(link.hasClass(module.CLASSES.jsAction));
                Y.Assert.areEqual("Request merge", link.get("text"));
                Y.Assert.isNull(
                  Y.one("#request-notifications .informational.message"),
                );
              }, 10);
            }, 10);
          },

          test_status_failure_during_poll_keeps_polling() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              this.runPoll();
              this.mockio.failure({ status: 500, responseText: "Boom" });
              this.wait(() => {
                this.assertInProgress();
                this.runPoll();
                Y.Assert.areEqual(
                  "ws.op=getMergeRequestStatus",
                  this.mockio.last_request.config.data,
                );
                this.respondJSON({ pending: false, merged: true });
                this.wait(() => {
                  const link = Y.one(".menu-link-request_merge");
                  Y.Assert.isTrue(link.hasClass(module.CLASSES.hidden));
                  Y.Assert.areEqual(0, this.poll_callbacks.length);
                }, 10);
              }, 10);
            }, 10);
          },

          /*
           * Visibility: polls pause while the page is hidden and
           * resume with an immediate check when it becomes visible.
           */

          test_poll_pauses_while_the_page_is_hidden() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.page_visible = true;
            const test = this;
            module.setup({
              lp_client: this.lp_client,
              schedule_poll(callback) {
                test.poll_callbacks.push(callback);
              },
              is_page_visible() {
                return test.page_visible;
              },
            });
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              Y.Assert.areEqual(1, this.poll_callbacks.length);
              this.page_visible = false;
              this.runPoll();
              this.respondJSON({ pending: true, merged: false });
              this.wait(() => {
                // No further poll is scheduled while the page is
                // hidden, and showing the page again runs an
                // immediate status check.
                Y.Assert.areEqual(0, this.poll_callbacks.length);
                this.page_visible = true;
                document.dispatchEvent(new Event("visibilitychange"));
                Y.Assert.areEqual(
                  "ws.op=getMergeRequestStatus",
                  this.mockio.last_request.config.data,
                );
                this.respondJSON({ pending: true, merged: false });
                this.wait(() => {
                  // Polling resumes once the page is visible again.
                  Y.Assert.areEqual(1, this.poll_callbacks.length);
                  this.assertInProgress();
                }, 10);
              }, 10);
            }, 10);
          },

          test_visibility_change_without_a_paused_poll_does_nothing() {
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();
            this.respondJSON({ pending: true, merged: false });

            this.wait(() => {
              Y.Assert.areEqual(1, this.poll_callbacks.length);
              document.dispatchEvent(new Event("visibilitychange"));
              Y.Assert.areEqual(1, this.mockio.requests.length);
              Y.Assert.areEqual(1, this.poll_callbacks.length);
            }, 10);
          },

          /*
           * Entry points: setup() guards and refresh() after new review
           * comments.
           */

          test_refresh_rechecks_permission_and_merge_criteria() {
            this.setup();
            this.respondJSON({
              can_be_merged: false,
              criteria: {
                is_approved: {
                  passed: false,
                  error: "Proposal has not been approved",
                },
              },
            });

            this.wait(() => {
              module.refresh();
              Y.Assert.areEqual(
                "ws.op=canIMerge",
                this.mockio.last_request.config.data,
              );
              this.respondJSON(true);
              this.wait(() => {
                Y.Assert.areEqual(
                  "ws.op=getMergeCriteria",
                  this.mockio.last_request.config.data,
                );
                this.respondJSON({ can_be_merged: true, criteria: {} });
                this.wait(() => {
                  const link = Y.one(".menu-link-request_merge");
                  Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
                  Y.Assert.isFalse(link.hasClass(module.CLASSES.blocked));
                  Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
                  Y.Assert.isTrue(link.hasClass(module.CLASSES.jsAction));
                  Y.Assert.areEqual("Request merge", link.get("text"));
                  Y.Assert.isNull(Y.one(".request-merge-blocked-reason"));
                  Y.Assert.isNull(
                    Y.one("#request-notifications .merge-blocked-message"),
                  );
                }, 10);
              }, 10);
            }, 10);
          },

          test_refresh_hides_the_link_when_the_user_may_not_merge() {
            this.setup();
            this.respondJSON({ can_be_merged: true, criteria: {} });

            this.wait(() => {
              module.refresh();
              Y.Assert.areEqual(
                "ws.op=canIMerge",
                this.mockio.last_request.config.data,
              );
              this.respondJSON(false);
              this.wait(() => {
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isTrue(link.hasClass(module.CLASSES.hidden));
                Y.Assert.areEqual(2, this.mockio.requests.length);
              }, 10);
            }, 10);
          },

          test_refresh_keeps_the_action_when_permission_check_fails() {
            this.setup();
            this.respondJSON({
              can_be_merged: false,
              criteria: {
                is_approved: {
                  passed: false,
                  error: "Proposal has not been approved",
                },
              },
            });

            this.wait(() => {
              module.refresh();
              Y.Assert.areEqual(
                "ws.op=canIMerge",
                this.mockio.last_request.config.data,
              );
              this.mockio.failure({ status: 500, responseText: "Boom" });
              this.wait(() => {
                const link = Y.one(".menu-link-request_merge");
                Y.Assert.isFalse(link.hasClass(module.CLASSES.hidden));
                Y.Assert.isTrue(link.hasClass(module.CLASSES.blocked));
                Y.Assert.areEqual("Merge blocked", link.get("text"));
                Y.Assert.areEqual(
                  "Proposal has not been approved",
                  Y.one(".request-merge-blocked-reason").get("text"),
                );
                Y.Assert.areEqual(2, this.mockio.requests.length);
              }, 10);
            }, 10);
          },

          test_merged_proposal_does_nothing() {
            LP.cache.context.queue_status = "Merged";
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();

            Y.Assert.areEqual(0, this.mockio.requests.length);
            Y.Assert.isFalse(
              Y.one(".menu-link-request_merge").hasClass(
                module.CLASSES.jsAction,
              ),
            );
          },

          test_disabled_feature_does_nothing() {
            LP.cache.merge_request_enabled = false;
            LP.cache.context.date_merge_requested = "2026-07-20T12:00:00+00:00";
            this.setup();

            const link = Y.one(".menu-link-request_merge");
            Y.Assert.areEqual(0, this.mockio.requests.length);
            Y.Assert.isFalse(link.hasClass(module.CLASSES.jsAction));
            Y.Assert.isTrue(link.hasClass(module.CLASSES.mergeProposal));
            this.clickLink();
            Y.Assert.areEqual(0, this.mockio.requests.length);
            Y.Assert.isNull(Y.one("#request-notifications .message"));
          },
        }),
      ),
    );
  },
  "0.1",
  {
    requires: [
      "node",
      "node-event-simulate",
      "test",
      "json",
      "lp.client",
      "lp.testing.mockio",
      "lp.code.branchmergeproposal.requestmerge",
    ],
  },
);
