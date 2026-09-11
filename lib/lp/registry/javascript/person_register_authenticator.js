/* Copyright 2026 Canonical Ltd. This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 */

/**
 * Hardware authenticator registration and revocation for person profiles.
 *
 * Registration has three stages:
 *
 * 1. POST beginWebAuthnRegistration to obtain creation options and store
 *    the challenge state in the current Launchpad session.
 * 2. Pass the converted publicKey options to
 *    navigator.credentials.create(). This is a browser/authenticator
 *    interaction, not another Launchpad API request.
 * 3. POST completeWebAuthnRegistration with the serialized credential
 *    and the user-chosen authenticator name.
 *
 * The begin and complete requests must preserve the relevant session
 * and refer to the same person's registration ceremony.
 *
 * The authenticator list is rendered server-side by:
 *
 *     IPerson/+register-authenticator-authenticators-list
 *
 * This module fetches that fragment after registration or revocation.
 * It does not construct authenticator table rows from API response data.
 *
 * @module lp.registry.javascript.person_register_authenticator
 */

/**
 * An existing credential that should be excluded from registration.
 *
 * Its binary ID is represented as base64url in the JSON response and
 * must be converted to an ArrayBuffer before calling WebAuthn.
 *
 * @typedef {Object} CredentialDescriptorJSON
 * @property {string} type - Always "public-key".
 * @property {string} id - Base64url-encoded credential ID.
 * @property {Array<string>} [transports] - Such as "usb" or "nfc".
 */

/**
 * The JSON representation of PublicKeyCredentialCreationOptions.
 *
 * This describes the publicKey member, not the outer response wrapper.
 * The challenge, user ID, and excluded credential IDs must be decoded
 * before passing these options to navigator.credentials.create().
 *
 * @typedef {Object} CreationOptionsJSON
 * @property {string} challenge - Base64url-encoded challenge.
 * @property {{id: string, name: string, displayName: string}} user
 *     The user ID is base64url-encoded.
 * @property {{id: string, name: string}} rp
 *     The relying party identity configured by Launchpad.
 * @property {Array<{type: string, alg: number}>} pubKeyCredParams
 * @property {Array<CredentialDescriptorJSON>} [excludeCredentials]
 * @property {{userVerification: string}} [authenticatorSelection]
 *     The current backend requests "required". Registration must perform
 *     user verification, for example using a security-key PIN.
 * @property {number} [timeout] - Browser timeout hint in milliseconds.
 */

/**
 * The response returned by beginWebAuthnRegistration().
 *
 * The backend calls Fido2Server.register_begin() and returns the creation
 * options as {"publicKey": {...}}. Binary values use the enabled fido2
 * JSON mapping, and _json_safe() converts remaining enum values into
 * JSON-compatible values.
 *
 * This is an exported write operation. It must be POSTed because it
 * stores registration challenge state in the session.
 *
 * @typedef {Object} CredentialCreationOptionsJSON
 * @property {CreationOptionsJSON} publicKey
 * @see lib/lp/registry/interfaces/person.py
 * @see lib/lp/services/webauthn/ceremony.py
 */

/**
 * The registration response sent to completeWebAuthnRegistration().
 *
 * Prefer the browser's credential.toJSON() when available. Otherwise,
 * serialize the required binary fields as unpadded base64url strings.
 *
 * clientExtensionResults must contain the returned extension values.
 * Assigning the getter function itself would cause JSON.stringify()
 * to omit the property.
 *
 * transports belongs inside response. complete_registration() reads
 * that member directly.
 *
 * @typedef {Object} CredentialResponseJSON
 * @property {string} id
 * @property {string} rawId - Base64url-encoded credential ID.
 * @property {string} type - Always "public-key".
 * @property {?string} [authenticatorAttachment]
 *     Usually "platform" or "cross-platform", when provided.
 * @property {Object} clientExtensionResults
 * @property {Object} response
 * @property {string} response.clientDataJSON - Base64url-encoded.
 * @property {string} response.attestationObject - Base64url-encoded.
 * @property {Array<string>} response.transports
 * @see lib/lp/registry/interfaces/person.py
 * @see lib/lp/services/webauthn/ceremony.py
 */

/**
 * Authenticator information used by the server-rendered list.
 *
 * The JavaScript does not consume this structure to build rows.
 * The fragment template renders the values and includes the ID and
 * name in the revoke button's data attributes.
 *
 * @typedef {Object} RegisteredAuthenticatorSummary
 * @property {number} id
 * @property {string} name
 * @property {string} date_created
 * @property {?string} date_last_used
 * @property {?string} date_revoked
 * @property {boolean} is_revoked
 * @see lib/lp/services/webauthn/interfaces.py
 * @see lib/lp/registry/templates/person-authenticators-list.pt
 */

YUI.add('lp.registry.javascript.person_register_authenticator', function(Y) {

    const namespace = Y.namespace(
        'lp.registry.javascript.person_register_authenticator');

    const client = new Y.lp.client.Launchpad();

    /* How long a non-error notification stays on screen. */
    const MESSAGE_TIMEOUT_MS = 5000;

    /*
     * Keep registration state separately from the button's disabled
     * property. A form submission or direct function call must not start
     * another ceremony while the current one is pending.
     */
    let registrationBusy = false;

    /*
     * Retain handles so initialization can be repeated without adding
     * duplicate event handlers, and tests can clean up their fixtures.
     */
    let eventHandles = [];
    let messageTimers = [];

    /*
     * The backend stores a single-use registration challenge with a
     * five-minute lifetime. If it has expired or is no longer available,
     * the user must start another registration ceremony.
     */
    const CHALLENGE_LOST_ERRORS = [
        'No WebAuthn registration is in progress.',
        'This registration challenge has expired.'
    ];

    /**
     * Read the global page cache lazily.
     *
     * window.LP is published by the page layout. It is distinct from the
     * YUI namespace Y.lp.
     *
     * Read it when needed because the module may attach before the
     * inline cache script runs, and tests replace it between cases.
     *
     * @returns {Object} The page cache, or an empty object.
     */
    function get_cache() {
        return (window.LP && window.LP.cache) || {};
    }

    /**
     * Decode an unpadded base64url string into an ArrayBuffer.
     *
     * Restore the standard base64 alphabet and any omitted padding
     * before calling atob().
     *
     * @param {string} base64url
     * @returns {ArrayBuffer}
     */
    function base64urlToArrayBuffer(base64url) {
        let base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
        while (base64.length % 4) {
            base64 += '=';
        }

        const binaryString = atob(base64);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }
        return bytes.buffer;
    }

    /**
     * Encode an ArrayBuffer as an unpadded base64url string.
     *
     * @param {ArrayBuffer} buffer
     * @returns {string}
     * @example
     * arrayBufferToBase64url(new Uint8Array([72, 101, 108]).buffer);
     * // => "SGVs"
     */
    function arrayBufferToBase64url(buffer) {
        const bytes = new Uint8Array(buffer);
        let binaryString = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binaryString += String.fromCharCode(bytes[i]);
        }
        return btoa(binaryString)
            .replace(/\+/g, '-')
            .replace(/\//g, '_')
            .replace(/=/g, '');
    }

    /**
     * Convert the publicKey member of the JSON registration options.
     *
     * The original options are not modified. Only the binary members
     * change representation; other options remain intact.
     *
     * @param {CreationOptionsJSON} options
     * @returns {Object} Options suitable for the browser WebAuthn API.
     */
    function convertCreationOptions(options) {
        const converted = JSON.parse(JSON.stringify(options));
        converted.challenge = base64urlToArrayBuffer(options.challenge);
        converted.user.id = base64urlToArrayBuffer(options.user.id);

        if (converted.excludeCredentials) {
            converted.excludeCredentials.forEach(function(credential) {
                credential.id = base64urlToArrayBuffer(credential.id);
            });
        }
        return converted;
    }

    /**
     * Convert a PublicKeyCredential into the JSON registration response.
     *
     * transports belongs inside response: complete_registration() reads
     * it from that location.
     *
     * @param {PublicKeyCredential} credential
     * @returns {CredentialResponseJSON}
     */
    function convertCredentialResponse(credential) {
        if (typeof credential.toJSON === 'function') {
            return credential.toJSON();
        }

        const response = credential.response;
        return {
            id: credential.id,
            rawId: arrayBufferToBase64url(credential.rawId),
            type: credential.type,
            authenticatorAttachment: credential.authenticatorAttachment,
            clientExtensionResults: credential.getClientExtensionResults ?
                credential.getClientExtensionResults() : {},
            response: {
                clientDataJSON:
                    arrayBufferToBase64url(response.clientDataJSON),
                attestationObject:
                    arrayBufferToBase64url(response.attestationObject),
                transports: response.getTransports ?
                    response.getTransports() : []
            }
        };
    }

    /**
     * Append a notification to the message area.
     *
     * Messages stack because an operation can succeed while the following
     * list refresh fails. Each non-error message has its own timer.
     * Error messages remain until the page is reloaded.
     *
     * Use text, not HTML, for messages that may contain server or browser
     * error details.
     *
     * @param {string} text
     * @param {string} [type='informational']
     * @returns {Y.Node|null}
     */
    function showMessage(text, type) {
        type = type || 'informational';
        const container = Y.one('#messages-container');
        if (!container) {
            return null;
        }

        const message = Y.Node.create('<div/>')
            .addClass(type)
            .addClass('message')
            .set('text', text);
        container.append(message);

        if (type !== 'error') {
            const timer = Y.later(MESSAGE_TIMEOUT_MS, null, function() {
                const index = messageTimers.indexOf(timer);
                if (index !== -1) {
                    messageTimers.splice(index, 1);
                }
                message.remove(true);
            });
            messageTimers.push(timer);
        }
        return message;
    }

    /**
     * Show an API error, with specific advice for a lost challenge.
     *
     * @param {string} prefix - Identifies the operation that failed.
     * @param {Object} response - The transport failure response.
     * @returns {Y.Node|null}
     */
    function showFailure(prefix, response) {
        let body = (response && response.responseText) || '';

        for (let i = 0; i < CHALLENGE_LOST_ERRORS.length; i++) {
            if (body.indexOf(CHALLENGE_LOST_ERRORS[i]) !== -1) {
                return showMessage(
                    'Your registration attempt timed out. Please click ' +
                    '"Register hardware authenticator" to start again.',
                    'error');
            }
        }

        if (!body) {
            body = response && response.status ?
                'HTTP ' + response.status :
                'The request could not be completed.';
        }
        return showMessage(prefix + ': ' + body, 'error');
    }

    /**
     * Update the registration state, button, and spinner together.
     *
     * @param {boolean} busy
     * @returns {void}
     */
    function setBusy(busy) {
        registrationBusy = busy;

        const button = Y.one('#register-button');
        const spinner = Y.one('#registration-spinner');
        if (button) {
            button.set('disabled', busy);
        }
        if (spinner) {
            spinner.setStyle('display', busy ? 'inline' : 'none');
        }
    }

    /**
     * Dispatch an exported write operation using Launchpad callbacks.
     *
     * named_post() performs an asynchronous request by default and
     * invokes config.on.success or config.on.failure.
     *
     * named_post_async() instead returns a Promise and replaces those
     * callbacks with its own resolve/reject handlers. Supplying callbacks
     * to that method while ignoring its Promise prevents the UI from
     * receiving either outcome.
     *
     * Catch synchronous errors as well, such as transport setup failures
     * before any request appears in the browser's Network panel.
     *
     * @param {string} operation - The exported operation name.
     * @param {Object} parameters - Parameters sent with the named POST.
     * @param {function} success - Receives the decoded operation result.
     * @param {function} failure - Receives the failure response.
     * @returns {void}
     */
    function postOperation(operation, parameters, success, failure) {
        const context = get_cache().context;
        if (!context || !context.self_link) {
            failure({
                responseText:
                    'This page is missing data it needs. Please reload.'
            });
            return;
        }

        try {
            client.named_post(context.self_link, operation, {
                parameters: parameters,
                on: {
                    success: success,
                    failure: function(id, response) {
                        failure(response);
                    }
                }
            });
        } catch (error) {
            failure({
                responseText: error.message || String(error)
            });
        }
    }

    /**
     * Prefer the server-provided authenticator fragment URL.
     *
     * If it is absent, derive the sibling endpoint from the registration
     * page path. Other paths deliberately have no fallback.
     *
     * @param {string} [pathname] Override the page path for unit tests.
     * @returns {string|null}
     */
    function getAuthenticatorsListURL(pathname) {
        const configuredURL = get_cache().authenticators_list_url;
        if (configuredURL) {
            return configuredURL;
        }

        if (pathname === undefined) {
            pathname = window.location.pathname;
        }

        const registrationPath = /\/\+register-authenticator\/?$/;
        if (!registrationPath.test(pathname)) {
            return null;
        }
        return pathname.replace(
            registrationPath,
            '/+register-authenticator-authenticators-list');
    }

    /**
     * Fetch the server-rendered authenticator list and replace its HTML.
     *
     * A refresh failure does not undo a successful registration or
     * revocation. Preserve the existing list and ask the user to reload.
     *
     * @param {function} [callback] Called after a successful replacement.
     * @returns {void}
     */
    function refreshAuthenticators(callback) {
        const container = Y.one('#authenticators-list');
        if (!container) {
            return;
        }

        const listURL = getAuthenticatorsListURL();
        if (!listURL) {
            showMessage(
                'Cannot find the authenticator list URL. ' +
                'Please reload the page.', 'error');
            return;
        }

        function refreshFailed() {
            showMessage(
                'Failed to refresh the list of authenticators. ' +
                'Please reload the page.', 'error');
        }

        try {
            Y.io(listURL, {
                on: {
                    success: function(id, response) {
                        container.setHTML(response.responseText);
                        if (callback) {
                            callback();
                        }
                    },
                    failure: refreshFailed
                }
            });
        } catch (error) {
            refreshFailed();
        }
    }

    /**
     * Revoke an authenticator and refresh the server-rendered list.
     *
     * DOM attributes contain strings, while the exported operation
     * declares authenticator_id as an Int.
     *
     * @param {number|string} authId
     * @returns {void}
     */
    function revokeAuthenticator(authId) {
        postOperation(
            'revokeWebAuthnRegistration',
            {authenticator_id: parseInt(authId, 10)},
            function() {
                showMessage('Hardware authenticator revoked successfully.');
                refreshAuthenticators();
            },
            function(response) {
                showFailure('Failed to revoke authenticator', response);
            });
    }

    /**
     * Ask for confirmation before revoking an authenticator.
     *
     * The authenticator name is inserted as text so it cannot become
     * markup in the confirmation overlay.
     *
     * @param {number|string} authId
     * @param {string} authName
     * @returns {void}
     */
    function confirmRevokeAuthenticator(authId, authName) {
        const message = 'Are you sure you want to revoke the authenticator "' +
            authName + '"? This action cannot be undone.';

        if (Y.lp.app && Y.lp.app.confirmationoverlay) {
            const overlay = new Y.lp.app.confirmationoverlay.ConfirmationOverlay({
                submit_fn: function() {
                    revokeAuthenticator(authId);
                },
                form_header: 'Revoke hardware authenticator',
                form_content: Y.Node.create('<p/>').set('text', message)
            });
            overlay.show();
        } else if (window.confirm(message)) {
            revokeAuthenticator(authId);
        }
    }

    /**
     * Restore the controls and explain a browser WebAuthn failure.
     *
     * @param {Error|DOMException} error
     * @returns {void}
     */
    function handleCredentialError(error) {
        setBusy(false);

        showMessage(error.message, 'error');
    }

    /**
     * Serialize the browser credential and complete registration.
     *
     * The exported response parameter is Text. Encode the credential as
     * JSON text, then JSON-encode that string for the form parameter.
     * lazr.restful decodes the outer layer before Text marshalling,
     * leaving valid JSON text for the backend's json.loads(response).
     *
     * @param {PublicKeyCredential} credential
     * @param {string} name - The name captured when registration began.
     * @returns {void}
     */
    function completeRegistration(credential, name) {
        let payload;
        try {
            payload = JSON.stringify(
                JSON.stringify(convertCredentialResponse(credential)));
        } catch (error) {
            setBusy(false);
            showMessage(
                'Failed to process credential: ' + error.message, 'error');
            return;
        }

        postOperation(
            'completeWebAuthnRegistration',
            {response: payload, name: name},
            function() {
                setBusy(false);
                const nameInput = Y.one('#authenticator-name');
                if (nameInput) {
                    nameInput.set('value', '');
                }
                showMessage(
                    'Hardware authenticator registered successfully!');
                refreshAuthenticators();
            },
            function(response) {
                setBusy(false);
                showFailure('Failed to complete registration', response);
            });
    }

    /**
     * Convert the server options and invoke the browser ceremony.
     *
     * Prefer the browser's JSON parser when available. Limit the fallback
     * catch to parsing: a failure from credential creation must not start
     * a second ceremony.
     *
     * navigator.credentials.create() returns a Promise. Handle both
     * Promise rejection and synchronous conversion/invocation errors.
     *
     * @param {CreationOptionsJSON} options
     * @param {string} name
     * @returns {void}
     */
    function performWebAuthnRegistration(options, name) {
        try {
            let publicKeyOptions;
            const credentialType = window.PublicKeyCredential;

            if (credentialType &&
                typeof credentialType.parseCreationOptionsFromJSON ===
                    'function') {
                try {
                    publicKeyOptions =
                        credentialType.parseCreationOptionsFromJSON(options);
                } catch (parseError) {
                    publicKeyOptions = convertCreationOptions(options);
                }
            } else {
                publicKeyOptions = convertCreationOptions(options);
            }

            navigator.credentials.create({
                publicKey: publicKeyOptions
            }).then(function(credential) {
                completeRegistration(credential, name);
            }).catch(handleCredentialError);
        } catch (error) {
            handleCredentialError(error);
        }
    }

    /**
     * Validate the name and start a new registration ceremony.
     *
     * Capture the name now so editing the input during the browser prompt
     * does not change the name submitted for this attempt.
     *
     * @returns {void}
     */
    function beginRegistration() {
        if (registrationBusy) {
            return;
        }

        const nameInput = Y.one('#authenticator-name');
        const name = nameInput ? nameInput.get('value').trim() : '';
        if (!name) {
            showMessage(
                'Please enter a name for your authenticator.', 'error');
            return;
        }

        setBusy(true);
        postOperation(
            'beginWebAuthnRegistration',
            {},
            function(options) {
                /*
                 * The API returns an outer publicKey wrapper. Validate it
                 * before entering the browser credential creation flow.
                 */
                if (!options || !options.publicKey) {
                    setBusy(false);
                    showMessage(
                        'The server returned invalid registration options.',
                        'error');
                    return;
                }
                performWebAuthnRegistration(options.publicKey, name);
            },
            function(response) {
                setBusy(false);
                showFailure('Failed to begin registration', response);
            });
    }

    /**
     * Detach handlers installed by initialize().
     *
     * @returns {void}
     */
    function detachHandlers() {
        eventHandles.forEach(function(handle) {
            handle.detach();
        });
        eventHandles = [];
    }

    /**
     * Detach page handlers and cancel notification timers.
     *
     * Tests call this before replacing their DOM fixtures.
     * This does not abort an outstanding browser or network operation.
     *
     * @returns {void}
     */
    function destroy() {
        detachHandlers();
        messageTimers.forEach(function(timer) {
            timer.cancel();
        });
        messageTimers = [];
        setBusy(false);
    }

    /**
     * Check browser support and page data, then attach event handlers.
     *
     * Feature detection establishes API availability only. The browser
     * still enforces certificate, origin, and RP ID checks when the
     * ceremony runs.
     *
     * @returns {void}
     */
    function initialize() {
        detachHandlers();

        if (typeof navigator === 'undefined' ||
            !navigator.credentials ||
            typeof navigator.credentials.create !== 'function') {
            const errorElement = Y.one('#browser-not-supported');
            const formElement = Y.one('#add-authenticator-form');
            if (errorElement) {
                errorElement.setStyle('display', 'block');
            }
            if (formElement) {
                formElement.setStyle('display', 'none');
            }
            return;
        }

        const context = get_cache().context;
        if (!context || !context.self_link) {
            showMessage(
                'This page is missing data it needs. Please reload.',
                'error');
            return;
        }

        /*
         * Delegate from the stable container so revoke handlers continue
         * working after refreshAuthenticators() replaces the table.
         */
        const list = Y.one('#authenticators-list');
        if (list) {
            eventHandles.push(list.delegate('click', function(event) {
                event.preventDefault();
                confirmRevokeAuthenticator(
                    event.currentTarget.getAttribute('data-auth-id'),
                    event.currentTarget.getAttribute('data-auth-name'));
            }, '.revoke-button'));
        }

        const form = Y.one('#add-authenticator-form');
        if (form) {
            eventHandles.push(form.on('submit', function(event) {
                event.preventDefault();
                beginRegistration();
            }));
        }

        const button = Y.one('#register-button');
        if (button) {
            eventHandles.push(button.on('click', function(event) {
                event.preventDefault();
                beginRegistration();
            }));
        }
    }

    Y.on('domready', initialize);

    namespace.initialize = initialize;
    namespace.destroy = destroy;
    namespace.showMessage = showMessage;
    namespace.getAuthenticatorsListURL = getAuthenticatorsListURL;
    namespace.refreshAuthenticators = refreshAuthenticators;
    namespace.beginRegistration = beginRegistration;
    namespace.revokeAuthenticator = revokeAuthenticator;
    namespace.base64urlToArrayBuffer = base64urlToArrayBuffer;
    namespace.arrayBufferToBase64url = arrayBufferToBase64url;
    namespace.convertCreationOptions = convertCreationOptions;
    namespace.convertCredentialResponse = convertCredentialResponse;

}, '0.1', {
    requires: [
        'node',
        'event',
        'event-delegate',
        'io-base',
        'yui-later',
        'lp.client',
        'lp.app.confirmationoverlay'
    ]
});
