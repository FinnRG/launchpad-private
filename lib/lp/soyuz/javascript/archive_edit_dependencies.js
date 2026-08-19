/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Enhancements for editing PPA dependencies.
 *
 * @module soyuz
 * @submodule archive_edit_dependencies
 * @requires node, event-key
 */
YUI.add('lp.soyuz.archive_edit_dependencies', function(Y) {

var namespace = Y.namespace('lp.soyuz.archive_edit_dependencies');

/* Announced in the status cell of a row that is marked for removal. */
namespace.PENDING_REMOVAL_TEXT = "Pending removal - save changes to apply.";

/*
 * Highlight (setting bold font-weight) the label for the selected option
 * in a given NodeList.  Assumes the input is contained by the label.
 *
 * @method highlight_checked
 */
namespace.highlight_checked = function(nodes) {
    nodes.each(function(input) {
        var label = Y.one(input.get('parentNode'));
        if (input.get('checked')) {
            label.setStyle('fontWeight', 'bold');
        }
        else {
            label.setStyle('fontWeight', 'normal');
        }
    });
};

/*
 * Highlight the selected options and keep the highlighting up to date as
 * the user clicks.
 *
 * @method setup_highlight_selected
 */
namespace.setup_highlight_selected = function() {
    // This page is rendered through the main_only macro, which has no
    // #mainarea element; scoping to it made the whole handler a no-op.
    var form_area = Y.one('#mainarea') || Y.one('#maincontent') ||
                    Y.one('body');
    if (form_area === null) {
        return;
    }
    // Highlight the selected radio button input on page load.
    var inputs = form_area.all('input.highlight-selected');
    namespace.highlight_checked(inputs);
    // Recalculate which options should be highlighted whenever one of
    // them is clicked.
    inputs.on('click', function(e) {
        namespace.highlight_checked(inputs);
    });
};

/*
 * Dark tooltip: shown on hover/focus via CSS.  Add a click toggle for
 * touch devices, and Escape to dismiss (WCAG 1.4.13).
 *
 * @method setup_help_tips
 */
namespace.setup_help_tips = function(table) {
    table.all('.help-tip').each(function(tip) {
        var trigger = tip.one('.help-trigger');
        if (trigger === null) {
            return;
        }
        trigger.on('click', function(e) {
            e.preventDefault();
            tip.toggleClass('help-tip-open');
        });
        tip.on('key', function(e) {
            tip.removeClass('help-tip-open');
            trigger.blur();
        }, 'esc');
    });
};

/*
 * The pin priority widget is rendered server-side as a plain text input
 * (IntWidget).  Upgrade it in place to a native number input with the
 * documented bounds and wire it to the help text.  The server-side
 * validation remains authoritative.
 *
 * @method setup_pin_priority_inputs
 */
namespace.setup_pin_priority_inputs = function(table) {
    // The limits are rendered from the same server-side constants used
    // by the form and model validation, so the client cannot drift from
    // them.
    var priority_min = table.getAttribute('data-priority-minimum');
    var priority_max = table.getAttribute('data-priority-maximum');
    table.all('.pin-priority-cell input').each(function(input) {
        if (input.get('type') === 'hidden') {
            return;
        }
        try {
            input.setAttribute('type', 'number');
        }
        catch (e) {
            // Ancient browsers refuse to mutate the type; the text
            // input keeps working, so carry on.
        }
        input.setAttribute('min', priority_min);
        input.setAttribute('max', priority_max);
        input.setAttribute('step', '1');
        input.setAttribute('inputmode', 'numeric');
        input.setAttribute('aria-describedby', 'pin-priority-help');
        input.addClass('pin-priority-input');
        // The column header alone is ambiguous when the control is read
        // out of context by a screen reader.
        var row = input.ancestor('tr');
        var name_node = (row === null) ? null : row.one('.dependency-name');
        if (name_node !== null) {
            input.setAttribute(
                'aria-label',
                'Pin priority for ' + name_node.get('text'));
        }
    });
};

/*
 * Reflect the pending-removal state of a single row.
 *
 * @method set_pending_removal
 */
namespace.set_pending_removal = function(row, pending) {
    if (row === null) {
        return;
    }
    var priority = row.one('.pin-priority-cell input.pin-priority-input');
    if (priority === null) {
        priority = row.one('.pin-priority-cell input:not([type=hidden])');
    }
    var cell = row.one('.pin-priority-cell');
    var mirror = row.one('.pin-priority-cell input.pin-priority-mirror');
    var status = row.one('.pending-removal-status');
    if (pending) {
        row.addClass('pending-removal');
        if (priority !== null) {
            // Disabled controls are not submitted, so keep a hidden
            // twin to preserve the posted value.
            if (mirror === null && cell !== null) {
                mirror = Y.Node.create('<input type="hidden" />');
                mirror.addClass('pin-priority-mirror');
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
        row.removeClass('pending-removal');
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

/*
 * Rows marked for removal get a subtle background, a strike-through
 * name, a dimmed disabled priority field and a politely announced
 * status.  The checkbox stays enabled so the removal can be undone.
 *
 * @method setup_removal_checkboxes
 */
namespace.setup_removal_checkboxes = function(table) {
    table.all('input.line-through-when-checked').each(function(checkbox) {
        // Restore state when the form is redisplayed after a validation
        // error.
        namespace.set_pending_removal(
            checkbox.ancestor('tr'), checkbox.get('checked'));
        var handler = function(e) {
            var target = e.currentTarget;
            namespace.set_pending_removal(
                target.ancestor('tr'), target.get('checked'));
        };
        checkbox.on('change', handler);
        checkbox.on('click', handler);
    });
};

/*
 * Wire up the whole page.
 *
 * @method setup_archive_edit_dependencies
 */
namespace.setup_archive_edit_dependencies = function() {
    namespace.setup_highlight_selected();
    var table = Y.one('#ppa-dependencies');
    if (table === null) {
        return;
    }
    namespace.setup_help_tips(table);
    namespace.setup_pin_priority_inputs(table);
    namespace.setup_removal_checkboxes(table);
};

}, "0.1", {"requires": ["node", "event-key"]});
