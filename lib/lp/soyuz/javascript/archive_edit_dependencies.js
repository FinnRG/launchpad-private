/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Enhancements for editing PPA dependencies.
 *
 * The +edit-dependencies form is rendered and validated entirely
 * server-side; this module only progressively enhances what is already
 * on the page:
 *
 *   - the label of the selected option in each radio group is
 *     emboldened, and stays up to date as the user clicks;
 *
 *   - the dark help tooltips, which CSS shows on hover and focus, also
 *     toggle on click for touch devices and dismiss on Escape
 *     (WCAG 1.4.13);
 *
 *   - each pin priority text input (rendered by IntWidget) is upgraded
 *     in place to a native number input carrying the server-side
 *     bounds, which only hints at the limits: the server-side
 *     validation remains authoritative;
 *
 *   - a row whose removal checkbox is ticked is shown as pending
 *     removal, with its priority field disabled but its posted value
 *     preserved, so the removal can still be undone before saving.
 *
 * None of this is required for the form to work; if the module fails
 * to load, the page still renders and submits correctly.
 *
 * The code below is organised in the same order: shared helpers, then
 * one section per enhancement, then the entry point.
 *
 * @module soyuz
 * @submodule archive_edit_dependencies
 * @requires node, event-key
 */
/** @import {YuiNode} from "../../app/javascript/yui" */
// utilities/js-deps requires a traditional factory and no trailing
// comma after the final argument.
YUI.add('lp.soyuz.archive_edit_dependencies', function (Y) {

const namespace = Y.namespace('lp.soyuz.archive_edit_dependencies');

/* Announced in the status cell of a row that is marked for removal. */
namespace.PENDING_REMOVAL_TEXT = "Pending removal - save changes to apply.";

/*
 * Shared helpers.
 */

/**
 * CSS class names the +edit-dependencies template renders, and which
 * this module reads or toggles.
 */
const CLASSES = {
    highlightSelected: 'highlight-selected',
    helpTip: 'help-tip',
    helpTrigger: 'help-trigger',
    helpTipOpen: 'help-tip-open',
    pinPriorityCell: 'pin-priority-cell',
    pinPriorityInput: 'pin-priority-input',
    pinPriorityMirror: 'pin-priority-mirror',
    dependencyName: 'dependency-name',
    pendingRemoval: 'pending-removal',
    pendingRemovalStatus: 'pending-removal-status',
    lineThroughWhenChecked: 'line-through-when-checked'
};

/* Id of the dependencies table this module enhances. */
const TABLE_ID = 'ppa-dependencies';

/* Id of the help text describing the accepted pin priorities. */
const PIN_PRIORITY_HELP_ID = 'pin-priority-help';

/**
 * The pin priority bounds the template renders onto the table.
 * @typedef {Object} PriorityBounds
 * @property {?string} minimum - Lowest priority the form accepts.
 * @property {?string} maximum - Highest priority the form accepts.
 */

/**
 * Read the pin priority bounds from the dependencies table.  They are
 * rendered from the same server-side constants used by the form and
 * model validation, so the client cannot drift from them.
 *
 * @param {YuiNode} table - The dependencies table.
 * @returns {PriorityBounds} The rendered bounds.
 */
const readPriorityBounds = (table) => ({
    minimum: table.getAttribute('data-priority-minimum'),
    maximum: table.getAttribute('data-priority-maximum')
});

/**
 * Find the visible pin priority input of a row, whether or not it has
 * already been upgraded to a number input.
 *
 * @param {YuiNode} row - The dependency row.
 * @returns {?YuiNode} The input, or null when the row has none.
 */
const findPriorityInput = (row) =>
    row.one(
        '.' + CLASSES.pinPriorityCell +
        ' input.' + CLASSES.pinPriorityInput) ||
    row.one(
        '.' + CLASSES.pinPriorityCell +
        ' input:not([type=hidden])');

/*
 * Highlighting the selected options.
 */

/**
 * Highlight (setting bold font-weight) the label for the selected
 * option in a given NodeList.  Assumes the input is contained by the
 * label.
 *
 * @param {Y.NodeList} nodes - The inputs whose labels to restyle.
 */
namespace.highlight_checked = (nodes) => {
    nodes.each((input) => {
        const label = input.ancestor('label');
        if (label === null) {
            return;
        }
        label.setStyle(
            'fontWeight', input.get('checked') ? 'bold' : 'normal');
    });
};

/**
 * Highlight the selected options and keep the highlighting up to date
 * as the user clicks.
 */
namespace.setup_highlight_selected = () => {
    // This page is rendered through the main_only macro, which has no
    // #mainarea element; scoping to it made the whole handler a no-op.
    const formArea = Y.one('#mainarea') || Y.one('#maincontent') ||
                     Y.one('body');
    if (formArea === null) {
        return;
    }
    // Highlight the selected radio button input on page load.
    const inputs = formArea.all('input.' + CLASSES.highlightSelected);
    namespace.highlight_checked(inputs);
    // Recalculate which options should be highlighted whenever one of
    // them is clicked.
    inputs.on('click', () => {
        namespace.highlight_checked(inputs);
    });
};

/*
 * Help tips.
 */

/**
 * Dark tooltip: shown on hover/focus via CSS.  Add a click toggle for
 * touch devices, and Escape to dismiss (WCAG 1.4.13).
 *
 * @param {YuiNode} table - The dependencies table.
 */
namespace.setup_help_tips = (table) => {
    table.all('.' + CLASSES.helpTip).each((tip) => {
        const trigger = tip.one('.' + CLASSES.helpTrigger);
        if (trigger === null) {
            return;
        }
        trigger.on('click', (e) => {
            e.preventDefault();
            tip.toggleClass(CLASSES.helpTipOpen);
        });
        tip.on('key', () => {
            tip.removeClass(CLASSES.helpTipOpen);
            trigger.blur();
        }, 'esc');
    });
};

/*
 * Pin priority inputs.
 */

/**
 * The pin priority widget is rendered server-side as a plain text
 * input (IntWidget).  Upgrade it in place to a native number input
 * with the documented bounds and wire it to the help text.  The
 * server-side validation remains authoritative.
 *
 * @param {YuiNode} table - The dependencies table.
 */
namespace.setup_pin_priority_inputs = (table) => {
    const bounds = readPriorityBounds(table);
    table.all('.' + CLASSES.pinPriorityCell + ' input').each((input) => {
        if (input.get('type') === 'hidden') {
            return;
        }
        input.setAttribute('type', 'number');
        input.setAttribute('min', bounds.minimum);
        input.setAttribute('max', bounds.maximum);
        input.setAttribute('step', '1');
        input.setAttribute('inputmode', 'numeric');
        input.setAttribute('aria-describedby', PIN_PRIORITY_HELP_ID);
        input.addClass(CLASSES.pinPriorityInput);
        // The column header alone is ambiguous when the control is read
        // out of context by a screen reader.
        const row = input.ancestor('tr');
        const nameNode =
            (row === null) ? null : row.one('.' + CLASSES.dependencyName);
        if (nameNode !== null) {
            input.setAttribute(
                'aria-label',
                'Pin priority for ' + nameNode.get('text'));
        }
    });
};

/*
 * Pending removal.
 */

/**
 * Reflect the pending-removal state of a single row.
 *
 * @param {?YuiNode} row - The dependency row, or null to do nothing.
 * @param {boolean} pending - Whether the row is marked for removal.
 */
namespace.set_pending_removal = (row, pending) => {
    if (row === null) {
        return;
    }
    const priority = findPriorityInput(row);
    const cell = row.one('.' + CLASSES.pinPriorityCell);
    const status = row.one('.' + CLASSES.pendingRemovalStatus);
    let mirror = row.one(
        '.' + CLASSES.pinPriorityCell +
        ' input.' + CLASSES.pinPriorityMirror);
    if (pending) {
        row.addClass(CLASSES.pendingRemoval);
        if (priority !== null) {
            // Disabled controls are not submitted, so keep a hidden
            // twin to preserve the posted value.
            if (mirror === null && cell !== null) {
                mirror = Y.Node.create('<input type="hidden" />');
                mirror.addClass(CLASSES.pinPriorityMirror);
                mirror.set('name', priority.get('name'));
                cell.append(mirror);
            }
            if (mirror !== null) {
                mirror.set('value', priority.get('value'));
            }
            priority.set('disabled', true);
            priority.setAttribute('aria-disabled', 'true');
        }
        if (status !== null) {
            status.setContent(namespace.PENDING_REMOVAL_TEXT);
        }
    }
    else {
        row.removeClass(CLASSES.pendingRemoval);
        if (priority !== null) {
            priority.set('disabled', false);
            priority.removeAttribute('aria-disabled');
        }
        if (mirror !== null) {
            mirror.remove(true);
        }
        if (status !== null) {
            status.setContent('');
        }
    }
};

/**
 * Rows marked for removal get a subtle background, a strike-through
 * name, a dimmed disabled priority field and a politely announced
 * status.  The checkbox stays enabled so the removal can be undone.
 *
 * @param {YuiNode} table - The dependencies table.
 */
namespace.setup_removal_checkboxes = (table) => {
    const selector = 'input.' + CLASSES.lineThroughWhenChecked;
    table.all(selector).each((checkbox) => {
        // Restore state when the form is redisplayed after a validation
        // error.
        namespace.set_pending_removal(
            checkbox.ancestor('tr'), checkbox.get('checked'));
        const handler = (e) => {
            const target = e.currentTarget;
            namespace.set_pending_removal(
                target.ancestor('tr'), target.get('checked'));
        };
        checkbox.on('change', handler);
        checkbox.on('click', handler);
    });
};

/*
 * Entry point.
 */

/**
 * Wire up the whole page.
 */
namespace.setup_archive_edit_dependencies = () => {
    namespace.setup_highlight_selected();
    const table = Y.one('#' + TABLE_ID);
    if (table === null) {
        return;
    }
    namespace.setup_help_tips(table);
    namespace.setup_pin_priority_inputs(table);
    namespace.setup_removal_checkboxes(table);
};

}, "0.1", {"requires": ["node", "event-key"]});
