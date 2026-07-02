#!/usr/bin/env bash
#
# Oduflow — import an Odoo.sh database into an Oduflow template.
#
# Run this INSIDE an Odoo.sh shell (production build). It finds the platform's
# latest daily backup (dump + filestore), then streams it to your Oduflow
# server, which restores it as a template. Nothing large is written to disk:
# the filestore is tarred on the fly and streamed straight to the upload.
#
# The upload is resumable: on a re-run it asks the server what it already has
# and only sends the missing pieces (manifest, dump, or individual filestore
# hash-directories).
#
#   curl -sSfL https://YOUR-ODUFLOW/import-odoo.sh | bash -s -- \
#        --server https://YOUR-ODUFLOW --token <TOKEN>
#
# The --token is minted by the "Import from Odoo.sh" button in the Oduflow
# dashboard and is valid for 15 minutes.
set -euo pipefail

SERVER=""
TOKEN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2:-}"; shift 2 ;;
        --server=*) SERVER="${1#*=}"; shift ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --token=*) TOKEN="${1#*=}"; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 20
            exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SERVER" ] || { echo "ERROR: --server is required" >&2; exit 2; }
[ -n "$TOKEN" ]  || { echo "ERROR: --token is required" >&2; exit 2; }
SERVER="${SERVER%/}"

# Resolve redirects up front (e.g. http -> https) so every upload hits the
# final URL directly — POSTs must not rely on redirect-following, which would
# drop the request body.
EFFECTIVE="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "${SERVER}/import-odoo.sh" 2>/dev/null || true)"
if [ -n "$EFFECTIVE" ]; then
    SERVER="${EFFECTIVE%/import-odoo.sh}"
fi

DB="${PGDATABASE:-}"
[ -n "$DB" ] || {
    echo "ERROR: PGDATABASE is not set — run this inside the Odoo.sh shell." >&2
    exit 2
}

BK="$HOME/backup.daily/${DB}_daily"
SQL_GZ="${BK}.sql.gz"
MANIFEST="${BK}.json"
FS="${BK}/home/odoo/data/filestore/${DB}"

if [ ! -f "$SQL_GZ" ] || [ ! -f "$MANIFEST" ]; then
    echo "ERROR: daily backup not found for database '$DB'." >&2
    echo "       Expected:" >&2
    echo "         $SQL_GZ" >&2
    echo "         $MANIFEST" >&2
    echo "       Daily backups are kept on production builds. If it is missing," >&2
    echo "       trigger a backup from the Odoo.sh dashboard and try again." >&2
    exit 3
fi
if [ ! -d "$FS" ]; then
    echo "ERROR: filestore directory not found: $FS" >&2
    exit 3
fi

AUTH="Authorization: Bearer ${TOKEN}"
API="${SERVER}/api/templates/import"

# ---- helpers ---------------------------------------------------------------

human() {  # bytes -> human readable
    awk -v b="${1:-0}" 'BEGIN{
        split("B KB MB GB TB", u, " "); i=1;
        while (b>=1024 && i<5){ b/=1024; i++ }
        if (i==1) printf "%d%s", b, u[i]; else printf "%.1f%s", b, u[i]
    }'
}

json_field() {  # file expr  (expr is python on obj `d`)
    python3 -c 'import sys,json
d=json.load(open(sys.argv[1]))
print('"$2"')' "$1" 2>/dev/null || true
}

# ---- status (drives resume) ------------------------------------------------

echo ">> Importing '$DB' into Oduflow at $SERVER"

STATUS_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE"' EXIT
if ! curl -fsS -H "$AUTH" "${API}/status" -o "$STATUS_FILE" 2>/dev/null; then
    echo "ERROR: could not reach $SERVER or token is invalid/expired." >&2
    echo "       Generate a fresh token from the dashboard and retry." >&2
    exit 4
fi

have_manifest="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("manifest") and 1 or ""')"
have_dump="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("dump") and 1 or ""')"
done_chunks=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("filestore_chunks",[]))') "

# ---- manifest --------------------------------------------------------------

if [ -z "$have_manifest" ]; then
    echo ">> uploading manifest"
    curl -fsS -H "$AUTH" -H "Content-Type: application/json" \
        --data-binary @"$MANIFEST" -X POST "${API}/manifest" >/dev/null
else
    echo ">> manifest already uploaded, skipping"
fi

# ---- dump ------------------------------------------------------------------

if [ -z "$have_dump" ]; then
    echo ">> uploading SQL dump ($(human "$(wc -c < "$SQL_GZ")"))"
    # -T streams from disk; --data-binary would buffer the whole file in RAM.
    curl -fsS -H "$AUTH" -H "Content-Type: application/gzip" \
        -T "$SQL_GZ" -X POST "${API}/dump" >/dev/null
else
    echo ">> SQL dump already uploaded, skipping"
fi

# ---- filestore (chunked by top-level hash directory) -----------------------

dir_bytes() {  # exact on GNU coreutils (du -sb); KB-approx fallback elsewhere
    local b
    if b="$(du -sb "$1" 2>/dev/null | cut -f1)"; then
        printf '%s' "$b"
    else
        b="$(du -sk "$1" | cut -f1)"
        printf '%s' "$((b * 1024))"
    fi
}

chunks=()
total_bytes=0
for path in "$FS"/*; do
    [ -d "$path" ] || continue
    name="$(basename "$path")"
    if [[ "$name" =~ ^[0-9a-f]{2}$ ]] || [ "$name" = "checklist" ]; then
        chunks+=("$name")
        total_bytes=$((total_bytes + $(dir_bytes "$path")))
    fi
done

done_bytes=0
remaining=()
for c in "${chunks[@]}"; do
    if [[ "$done_chunks" == *" $c "* ]]; then
        done_bytes=$((done_bytes + $(dir_bytes "$FS/$c")))
    else
        remaining+=("$c")
    fi
done

progress() {  # done total label
    local pct=0
    [ "$2" -gt 0 ] && pct=$(( $1 * 100 / $2 ))
    printf '\r>> filestore [%3d%%] %s / %s  %-14s\033[K' \
        "$pct" "$(human "$1")" "$(human "$2")" "$3" >&2
}

upload_chunk() {  # name
    local c="$1" tries=0
    while :; do
        # -T - streams the tar as it is produced (chunked transfer encoding);
        # --data-binary @- would buffer the whole chunk in RAM first.
        if tar -C "$FS" -cf - "$c" \
            | curl -fsS -H "$AUTH" -H "Content-Type: application/x-tar" \
                   -T - -X POST "${API}/filestore?chunk=${c}" >/dev/null; then
            return 0
        fi
        tries=$((tries + 1))
        [ "$tries" -ge 3 ] && return 1
        sleep 2
    done
}

echo ">> filestore: $(human "$total_bytes") in ${#chunks[@]} chunks (${#remaining[@]} to upload)"
for c in "${remaining[@]}"; do
    progress "$done_bytes" "$total_bytes" "uploading $c"
    if ! upload_chunk "$c"; then
        echo >&2
        echo "ERROR: failed to upload filestore chunk '$c' after retries." >&2
        echo "       Re-run the same command to resume from here." >&2
        exit 5
    fi
    done_bytes=$((done_bytes + $(dir_bytes "$FS/$c")))
    progress "$done_bytes" "$total_bytes" "done $c"
done
progress "$total_bytes" "$total_bytes" "complete"
echo >&2

# ---- finalize --------------------------------------------------------------

echo ">> finalizing (restoring database — this can take a few minutes)…"
RESULT_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE" "$RESULT_FILE"' EXIT
# --fail-with-body keeps the server's error body on HTTP >= 400 (plain -f
# would discard it, hiding the reason the riskiest step failed).
if ! curl -sS --fail-with-body -H "$AUTH" -X POST "${API}/finalize" -o "$RESULT_FILE"; then
    echo "ERROR: finalize request failed:" >&2
    cat "$RESULT_FILE" >&2 || true
    echo >&2
    exit 6
fi

python3 -c 'import sys,json
d=json.load(open(sys.argv[1]))
if not d.get("ok"):
    print(">> ERROR:", d.get("error","unknown error")); sys.exit(1)
r=d.get("result",{})
print(">> Done — template ready:", r.get("template_name"))
print("   DB:", r.get("template_db"), " restore:", r.get("restore_seconds"), "s")' "$RESULT_FILE"
