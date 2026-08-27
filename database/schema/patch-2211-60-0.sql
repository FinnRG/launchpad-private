-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

CREATE TABLE RegisteredAuthenticator (
    id serial PRIMARY KEY,
    owner integer NOT NULL REFERENCES person,
    credential_id bytea NOT NULL,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL,
    aaguid bytea NOT NULL,
    attestation_format text NOT NULL,
    transports text[],
    name text NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    date_last_used timestamp without time zone,
    date_revoked timestamp without time zone,
    revoked_by integer REFERENCES person
);

CREATE UNIQUE INDEX registeredauthenticator__credential_id__key
    ON RegisteredAuthenticator (credential_id);
CREATE INDEX registeredauthenticator__owner__idx
    ON RegisteredAuthenticator (owner);
CREATE INDEX registeredauthenticator__revoked_by__idx
    ON RegisteredAuthenticator (revoked_by);

COMMENT ON TABLE RegisteredAuthenticator IS 'A FIDO2/WebAuthn authenticator registered by a person.';
COMMENT ON COLUMN RegisteredAuthenticator.owner IS 'The person who registered this authenticator.';
COMMENT ON COLUMN RegisteredAuthenticator.credential_id IS 'The credential ID assigned to this authenticator by itself.';
COMMENT ON COLUMN RegisteredAuthenticator.public_key IS 'The COSE-encoded public key for this authenticator.';
COMMENT ON COLUMN RegisteredAuthenticator.sign_count IS 'The last signature counter value seen from this authenticator, used to detect cloned authenticators.';
COMMENT ON COLUMN RegisteredAuthenticator.aaguid IS 'The AAGUID identifying the make and model of this authenticator.';
COMMENT ON COLUMN RegisteredAuthenticator.attestation_format IS 'The attestation statement format used when this authenticator was registered.';
COMMENT ON COLUMN RegisteredAuthenticator.transports IS 'Transport hints (e.g. usb, nfc) reported by the authenticator at registration time.';
COMMENT ON COLUMN RegisteredAuthenticator.name IS 'A name chosen by the owner to help them tell their registered authenticators apart.';
COMMENT ON COLUMN RegisteredAuthenticator.date_created IS 'When this authenticator was registered.';
COMMENT ON COLUMN RegisteredAuthenticator.date_last_used IS 'When this authenticator was last used to sign a challenge.';
COMMENT ON COLUMN RegisteredAuthenticator.date_revoked IS 'When this authenticator was revoked, if it has been.';
COMMENT ON COLUMN RegisteredAuthenticator.revoked_by IS 'The person who revoked this authenticator, if any.';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 60, 0);
