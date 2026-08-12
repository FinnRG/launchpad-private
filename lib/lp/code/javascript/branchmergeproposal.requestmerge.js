/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Request a merge from a branch merge proposal page.
 *
 * The "Request merge" action is a small state machine, driven by
 * web-service calls:
 *
 *   setup()
 *     proposal already merged    -> done
 *     date_merge_requested set   -> POLLING
 *     otherwise                  -> CHECKING
 *
 *   CHECKING   getMergeCriteria
 *     can be merged              -> READY
 *     otherwise                  -> BLOCKED
 *
 *   READY      link enabled; a click posts requestMerge
 *     request accepted           -> POLLING
 *     request failed             -> READY     (error notification)
 *
 *   POLLING    getMergeRequestStatus every POLL_INTERVAL while the
 *     page is visible
 *     still pending              -> POLLING   (spinner + notification)
 *     merged                     -> MERGED
 *     no longer pending          -> CHECKING
 *
 *   BLOCKED    disabled link listing the failed criteria
 *   MERGED     terminal; link hidden, success notification
 *
 * refresh() re-enters CHECKING when new review comments are posted
 * (for example an approval unblocking the merge), or hides the link
 * if the user may no longer merge. A pending request survives page
 * reloads via date_merge_requested in the JSON cache; the blocked
 * warning notification is only shown when a request watched in this
 * session ends without a merge. Status polls pause while the page is
 * hidden and resume with an immediate check when it becomes visible
 * again.
 *
 * The code below is organised in the same order: shared helpers,
 * then one section per state, then the entry points.
 *
 * @module lp.code.branchmergeproposal.requestmerge
 * @requires node, json, lp.client
 */

/** @import {LaunchpadClient as LPClient} from "../../app/javascript/yui" */
/** @import {YuiNode} from "../../app/javascript/yui" */
/**
 * Scheduler that runs a callback when the next status poll is due.
 * @typedef {function(function(): void): void} PollScheduler
 */

/**
 * Optional overrides for setup(), used by tests.
 * @typedef {Object} SetupConfig
 * @property {LPClient} [lp_client] - Web-service client.
 * @property {PollScheduler} [schedule_poll] - Poll scheduler.
 * @property {function(): boolean} [is_page_visible] - Whether polls
 *     may run; defaults to the Page Visibility API.
 */

// utilities/js-deps requires a traditional factory and no trailing
// comma after the final argument.
YUI.add(
  "lp.code.branchmergeproposal.requestmerge",
  function (Y) {
    const namespace = Y.namespace("lp.code.branchmergeproposal.requestmerge");

    const IN_PROGRESS_MESSAGE =
      "A merge has been requested for this proposal and is in progress. " +
      "Reload this page later to see the result.";
    const BLOCKED_MESSAGE = "Merge is currently blocked";
    const POLL_INTERVAL = 5000;
    namespace.IN_PROGRESS_MESSAGE = IN_PROGRESS_MESSAGE;
    namespace.BLOCKED_MESSAGE = BLOCKED_MESSAGE;
    namespace.POLL_INTERVAL = POLL_INTERVAL;

    /**
     * CSS class names applied to the action link, its spinner, and the
     * blocked/in-progress notifications.
     */
    const CLASSES = {
      sprite: "sprite",
      mergeProposal: "merge-proposal",
      loading: "request-merge-loading",
      inProgress: "request-merge-in-progress",
      blocked: "request-merge-blocked",
      blockedReason: "request-merge-blocked-reason",
      spinner: "request-merge-spinner",
      blockedMessage: "merge-blocked-message",
      inProgressMessage: "merge-in-progress-message",
      noLink: "nolink",
      jsAction: "js-action",
      hidden: "hidden",
      lesser: "lesser",
    };
    namespace.CLASSES = CLASSES;

    /**
     * One entry in the getMergeCriteria result.
     * @typedef {Object} MergeCriterion
     * @property {boolean} passed - Whether the criterion is met.
     * @property {boolean} is_required - Whether it must pass before the
     *     merge may be forced.
     * @property {string} [error] - Reason shown when the criterion
     *     failed.
     * @see BranchMergeProposal.checkMergeCriteria in
     *     lib/lp/code/model/branchmergeproposal.py; criterion names come
     *     from lp.code.enums.MergeProposalCriteria.
     */

    /**
     * Result of the getMergeCriteria web-service call.
     * @typedef {Object} MergeCriteria
     * @property {boolean} can_be_merged - All criteria are met.
     * @property {boolean} can_be_force_merged - All required criteria
     *     are met.
     * @property {Object.<string, MergeCriterion>} criteria - Each
     *     criterion, keyed by name.
     * @see BranchMergeProposal.getMergeCriteria in
     *     lib/lp/code/model/branchmergeproposal.py.
     */

    /**
     * Result of the getMergeRequestStatus web-service call.
     * @typedef {Object} MergeRequestStatus
     * @property {boolean} pending - A request was made recently and the
     *     proposal is not merged yet.
     * @property {boolean} merged - The proposal has been merged.
     * @see BranchMergeProposal.getMergeRequestStatus in
     *     lib/lp/code/model/branchmergeproposal.py; pending is derived
     *     from date_merge_requested and the pending window.
     */

    /**
     * Web-service client used by every request; set once in setup().
     * @type {?LPClient}
     */
    let activeClient = null;

    /**
     * The "Request merge" anchor in the global actions menu, or null when
     * the user cannot request merges. Set in setup().
     * @type {?YuiNode}
     */
    let activeLink = null;

    /**
     * Scheduler for status polls; defaults to window.setTimeout with
     * POLL_INTERVAL, replaceable through setup() for tests.
     * @type {?PollScheduler}
     */
    let schedulePoll = null;

    /**
     * Whether the in-progress notification for the current merge request
     * has already been shown, so polls do not repeat it and an in-session
     * failure can be told apart from a stale request on page load.
     * @type {boolean}
     */
    let pendingNotificationShown = false;

    /**
     * Whether status polls may run; defaults to the Page Visibility
     * API, replaceable through setup() for tests.
     * @type {?function(): boolean}
     */
    let isPageVisible = null;

    /**
     * True when a status poll was skipped because the page is hidden;
     * the next visibilitychange to visible then polls immediately.
     * @type {boolean}
     */
    let pollPaused = false;

    /**
     * The visibilitychange handler registered by setup(), kept so a
     * repeated setup() replaces it instead of stacking listeners.
     * @type {?function(): void}
     */
    let onVisibilityChange = null;

    /*
     * Shared helpers.
     */

    /**
     * Display a Launchpad notification.
     * @param {number} level - Notification level (20 info, 30 warning,
     *     40 error).
     * @param {string} message - Text to display.
     */
    const notify = (level, message) => {
      Y.lp.client.display_notifications(Y.JSON.stringify([[level, message]]));
    };

    /**
     * Replace the link content with a spinner and a progress label.
     * @param {?YuiNode} link - The action link, or null to do nothing.
     * @param {string} message - Label shown next to the spinner.
     */
    const showLoading = (link, message) => {
      if (link === null) {
        return;
      }
      link.removeClass(CLASSES.sprite);
      link.removeClass(CLASSES.mergeProposal);
      link.addClass(CLASSES.loading);
      link.setHTML(
        '<img class="' +
          CLASSES.spinner +
          '" src="/@@/spinner" alt=""/>' +
          message,
      );
    };

    /*
     * CHECKING: ask the web service whether the merge can be requested,
     * then move to READY or BLOCKED.
     */

    /**
     * Ask the web service whether the proposal can be merged, then either
     * enable the link or show it as blocked.
     * @param {?YuiNode} link - The action link, or null to do nothing.
     * @param {LPClient} lpClient - Web-service client.
     * @param {boolean} notifyBlocked - Passed on to showBlocked().
     */
    const checkMergeCriteria = async (link, lpClient, notifyBlocked) => {
      if (link === null) {
        return;
      }
      showLoading(link, "Checking merge...");
      try {
        /** @type {MergeCriteria} */
        const status = await lpClient.named_get_async(
          LP.cache.context.self_link,
          "getMergeCriteria",
          { parameters: {} },
        );
        if (status.can_be_merged) {
          clearBlockedNotification();
          wireClick(link, lpClient);
        } else {
          showBlocked(link, status, notifyBlocked);
        }
      } catch {
        // The merge endpoint still enforces all criteria.
        wireClick(link, lpClient);
      }
    };

    /*
     * READY: the link is enabled and a click posts requestMerge, moving
     * to POLLING on success.
     */

    /**
     * Reset the link to its normal, clickable "Request merge" state,
     * clearing any loading, blocked, or in-progress presentation.
     * @param {?YuiNode} link - The action link, or null to do nothing.
     */
    const showMergeAction = (link) => {
      if (link === null) {
        return;
      }
      link.removeClass(CLASSES.blocked);
      clearBlockedReason();
      link.removeClass(CLASSES.loading);
      link.removeClass(CLASSES.inProgress);
      link.removeClass(CLASSES.noLink);
      link.removeAttribute("aria-disabled");
      link.removeAttribute("title");
      link.removeClass(CLASSES.hidden);
      /** @type {?string} */
      const href = link.getData("request-merge-href");
      if (href) {
        link.setAttribute("href", href);
      }
      link.addClass(CLASSES.sprite);
      link.addClass(CLASSES.mergeProposal);
      link.addClass(CLASSES.jsAction);
      link.set("text", "Request merge");
    };

    /**
     * Enable the link and attach the click handler that posts
     * requestMerge and starts polling on success.
     * @param {YuiNode} link - The action link.
     * @param {LPClient} lpClient - Web-service client.
     */
    const wireClick = (link, lpClient) => {
      showMergeAction(link);
      if (link.getData("request-merge-wired")) {
        return;
      }
      link.setData("request-merge-wired", true);
      link.on("click", async (e) => {
        e.preventDefault();
        if (
          link.hasClass(CLASSES.loading) ||
          link.hasClass(CLASSES.inProgress) ||
          link.hasClass(CLASSES.blocked)
        ) {
          return;
        }
        showLoading(link, "Requesting merge...");
        try {
          const result = await lpClient.named_post_async(
            LP.cache.context.self_link,
            "requestMerge",
            { parameters: {} },
          );
          showInProgress(link);
          notify(
            20,
            typeof result === "string" && result !== ""
              ? result
              : "Merge successfully queued",
          );
          pendingNotificationShown = true;
          scheduleStatusPoll();
        } catch (response) {
          showMergeAction(link);
          notify(
            40,
            response.responseText || "Request to merge this proposal failed.",
          );
        }
      });
    };

    /*
     * BLOCKED: the link is disabled and the failed criteria are listed
     * in a small message below it.
     */

    /** Remove any "merge blocked" notification from the page. */
    const clearBlockedNotification = () => {
      Y.all("#request-notifications ." + CLASSES.blockedMessage).each(
        (notification) => notification.remove(true),
      );
    };

    /** Remove any small blocked-reason text below the action link. */
    const clearBlockedReason = () => {
      Y.all("." + CLASSES.blockedReason).each((reason) => reason.remove(true));
    };

    /**
     * Disable the link as "Merge blocked" and list the failed criteria
     * in a small message below it.
     * @param {YuiNode} link - The action link.
     * @param {MergeCriteria} status - getMergeCriteria result; failed
     *     entries in status.criteria provide the reasons.
     * @param {boolean} notifyBlocked - Also show a warning notification;
     *     only used when a request watched in this session fails.
     */
    const showBlocked = (link, status, notifyBlocked) => {
      const reasons = Object.values(status.criteria || {})
        .filter((criterion) => criterion.passed === false && criterion.error)
        .map((criterion) => criterion.error);
      const message =
        reasons.length > 0
          ? BLOCKED_MESSAGE + ": " + reasons.join("; ")
          : BLOCKED_MESSAGE + ".";
      if (notifyBlocked) {
        clearBlockedNotification();
        notify(30, message);
        const notifications = Y.all("#request-notifications .warning.message");
        notifications
          .item(notifications.size() - 1)
          .addClass(CLASSES.blockedMessage);
      }
      showMergeAction(link);
      link.addClass(CLASSES.blocked);
      link.removeClass(CLASSES.jsAction);
      link.addClass(CLASSES.noLink);
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("title", message);
      link.set("text", "Merge blocked");
      if (reasons.length > 0) {
        const reasonNode = Y.Node.create(
          '<div class="' +
            CLASSES.blockedReason +
            " " +
            CLASSES.lesser +
            '"></div>',
        );
        reasonNode.set("text", reasons.join("; "));
        link.insert(reasonNode, "after");
      }
    };

    /*
     * POLLING: a merge request is pending; poll getMergeRequestStatus
     * every POLL_INTERVAL until it is merged or no longer pending.
     */

    /** Remove any "merge in progress" notification from the page. */
    const clearInProgressNotification = () => {
      Y.all("#request-notifications ." + CLASSES.inProgressMessage).each(
        (notification) => notification.remove(true),
      );
    };

    /**
     * Disable the link and mark the merge request as in progress.
     * @param {?YuiNode} link - The action link, or null to do nothing.
     */
    const showInProgress = (link) => {
      if (link === null) {
        return;
      }
      link.removeClass(CLASSES.sprite);
      link.removeClass(CLASSES.mergeProposal);
      link.removeClass(CLASSES.loading);
      link.addClass(CLASSES.inProgress);
      link.removeClass(CLASSES.jsAction);
      link.addClass(CLASSES.noLink);
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("title", IN_PROGRESS_MESSAGE);
      link.setHTML(
        '<img class="' +
          CLASSES.spinner +
          '" src="/@@/spinner" ' +
          'alt="In progress"/>' +
          "Merge in progress",
      );
    };

    /**
     * Show the in-progress notification and disable the link, for a
     * pending request discovered on page load.
     * @param {?YuiNode} link - The action link, or null to skip it.
     */
    const showPending = (link) => {
      clearInProgressNotification();
      notify(20, IN_PROGRESS_MESSAGE);
      const notifications = Y.all(
        "#request-notifications .informational.message",
      );
      notifications
        .item(notifications.size() - 1)
        .addClass(CLASSES.inProgressMessage);
      showInProgress(link);
    };

    /**
     * Schedule the next getMergeRequestStatus poll, unless the page is
     * hidden; a hidden page polls again as soon as it becomes visible.
     */
    const scheduleStatusPoll = () => {
      if (!isPageVisible()) {
        pollPaused = true;
        return;
      }
      schedulePoll(() => getMergeRequestStatus(true));
    };

    /**
     * Fetch the {@link MergeRequestStatus} and move to the matching
     * state: keep polling while pending, announce a merge, or re-check
     * the merge criteria once the request is no longer pending.
     * @param {boolean} isPoll - True on scheduled polls; false on page
     *     load, which also shows the loading spinner.
     */
    const getMergeRequestStatus = async (isPoll) => {
      if (!isPoll) {
        showLoading(activeLink, "Checking merge...");
      }
      try {
        /** @type {MergeRequestStatus} */
        const status = await activeClient.named_get_async(
          LP.cache.context.self_link,
          "getMergeRequestStatus",
          { parameters: {} },
        );
        if (status.pending) {
          if (pendingNotificationShown) {
            showInProgress(activeLink);
          } else {
            showPending(activeLink);
            pendingNotificationShown = true;
          }
          scheduleStatusPoll();
        } else {
          const wasPending = pendingNotificationShown;
          pendingNotificationShown = false;
          if (status.merged) {
            showMerged(activeLink);
          } else {
            clearInProgressNotification();
            // Only explain a blocked merge when we watched
            // the request fail in this session; a plain
            // page load stays quiet.
            await checkMergeCriteria(activeLink, activeClient, wasPending);
          }
        }
      } catch {
        if (pendingNotificationShown) {
          scheduleStatusPoll();
        } else {
          await checkMergeCriteria(activeLink, activeClient, false);
        }
      }
    };

    /*
     * MERGED: terminal state; the proposal was merged and the action
     * link is hidden.
     */

    /**
     * Announce that the proposal was merged and hide the action link.
     * @param {?YuiNode} link - The action link, or null to skip it.
     */
    const showMerged = (link) => {
      clearInProgressNotification();
      if (link !== null) {
        showMergeAction(link);
        link.addClass(CLASSES.hidden);
      }
      notify(20, "The proposal has now been marked as merged.");
    };

    /*
     * Entry points.
     */

    /**
     * Wire up the "Request merge" action on page load.
     * @param {SetupConfig} [config] - Optional overrides for tests.
     */
    namespace.setup = (config) => {
      if (!LP.cache.merge_request_enabled) {
        return;
      }
      activeClient =
        (config && config.lp_client) || new Y.lp.client.Launchpad();
      activeLink = Y.one("#global-actions a.menu-link-request_merge");
      schedulePoll =
        (config && config.schedule_poll) ||
        ((callback) => {
          window.setTimeout(callback, POLL_INTERVAL);
        });
      pendingNotificationShown = false;
      isPageVisible =
        (config && config.is_page_visible) || (() => !document.hidden);
      pollPaused = false;
      if (onVisibilityChange !== null) {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
      onVisibilityChange = () => {
        if (pollPaused && isPageVisible()) {
          pollPaused = false;
          getMergeRequestStatus(true);
        }
      };
      document.addEventListener("visibilitychange", onVisibilityChange);
      if (activeLink !== null) {
        activeLink.setData(
          "request-merge-href",
          activeLink.getAttribute("href"),
        );
      }
      if (LP.cache.context.queue_status === "Merged") {
        return;
      }
      if (
        LP.cache.context.date_merge_requested !== null &&
        LP.cache.context.date_merge_requested !== undefined
      ) {
        getMergeRequestStatus(false);
        return;
      }
      checkMergeCriteria(activeLink, activeClient, false);
    };

    /**
     * Re-evaluate the action after new review comments are posted (for
     * example an approval unblocking the merge). Hides the link when the
     * user may no longer merge; does nothing while a request is in
     * progress.
     */
    namespace.refresh = async () => {
      if (
        !LP.cache.merge_request_enabled ||
        activeClient === null ||
        activeLink === null ||
        activeLink.hasClass(CLASSES.inProgress) ||
        LP.cache.context.queue_status === "Merged"
      ) {
        return;
      }
      try {
        /** @type {boolean} */
        const canMerge = await activeClient.named_get_async(
          LP.cache.context.self_link,
          "canIMerge",
          { parameters: {} },
        );
        if (canMerge) {
          await checkMergeCriteria(activeLink, activeClient, false);
        } else {
          activeLink.addClass(CLASSES.hidden);
        }
      } catch {
        // Keep the current action state when permission cannot be checked.
      }
    };
  },
  "0.1",
  { requires: ["node", "json", "lp.client"] }
);
