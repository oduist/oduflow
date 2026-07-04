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
# and only sends the missing pieces (manifest, dump, individual filestore
# hash-directories, or addon repositories).
#
#   curl -sSfL https://YOUR-ODUFLOW/import-odoo.sh | bash -s -- \
#        --server https://YOUR-ODUFLOW --token <TOKEN>
#
# Optional flags (usually set for you by the dashboard checkboxes) also bring
# over the addons Odoo.sh ran with, beyond the standard modules in the image:
#   --with-enterprise    download Odoo Enterprise into a local extra-addons repo
#   --with-themes        download Odoo Themes into a local extra-addons repo
#   --with-extra-addons  add the extra repos (OCA etc.): reachable ones are
#                        cloned from their origin (stay updatable), private ones
#                        are downloaded as local extra-addons repos
#
# The --token is minted by the "Import from Odoo.sh" button in the Oduflow
# dashboard and is valid for 15 minutes.
set -euo pipefail

SERVER=""
TOKEN=""
WITH_ENTERPRISE=0
WITH_THEMES=0
WITH_EXTRA=0
while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2:-}"; shift 2 ;;
        --server=*) SERVER="${1#*=}"; shift ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --token=*) TOKEN="${1#*=}"; shift ;;
        --with-enterprise) WITH_ENTERPRISE=1; shift ;;
        --with-themes) WITH_THEMES=1; shift ;;
        --with-extra-addons) WITH_EXTRA=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 28
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
if ! curl -sS --fail-with-body -H "$AUTH" "${API}/status" -o "$STATUS_FILE" 2>/dev/null; then
    echo "ERROR: could not reach $SERVER or token is invalid/expired." >&2
    [ -s "$STATUS_FILE" ] && { echo "       Server said:" >&2; head -c 300 "$STATUS_FILE" >&2; echo >&2; }
    echo "       Generate a fresh token from the dashboard and retry." >&2
    exit 4
fi

# Hard gate: the status body must be Oduflow's JSON with ok=true. Anything
# else (a proxy page, a redirect body, an HTML error) means the uploads
# would go nowhere — abort NOW instead of "uploading" gigabytes into a 3xx.
if [ -z "$(json_field "$STATUS_FILE" 'd.get("ok") and 1 or ""')" ]; then
    echo "ERROR: unexpected response from ${API}/status:" >&2
    head -c 300 "$STATUS_FILE" >&2; echo >&2
    echo "       Check that --server points at your Oduflow dashboard URL" >&2
    echo "       (https, no proxy pages in between), then retry." >&2
    exit 4
fi

have_manifest="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("manifest") and 1 or ""')"
have_dump="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("dump") and 1 or ""')"
done_chunks=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("filestore_chunks",[]))') "
done_addons=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("addons",[]))') "
done_remote=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("remote_addons",[]))') "

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

# ---- addons (optional: enterprise / themes / extra repos) ------------------
#
# The daily backup holds only the database and filestore; the addons Odoo.sh
# runs with live on the build filesystem. When asked (--with-*), inspect the
# running server's --addons-path, classify each entry, and bring over what the
# image doesn't already ship. Reachable extra repos are announced by their
# origin (Oduflow clones them, keeping them updatable); everything else is
# tarred and streamed as a local (remote-less) extra-addons repo.

detect_addons_path() {
    # Prefer the live odoo-bin command line (source of truth), fall back to the
    # generated odoo.conf. Only the "--addons-path=..." (=-joined) form is used
    # by Odoo.sh.
    local ap
    # -e is essential: without it ps only lists processes sharing the caller's
    # controlling terminal, and the odoo-bin daemon (started outside the SSH
    # tty) would never be seen — detection would always fall through to
    # odoo.conf. Verified empirically on an Odoo.sh shell.
    ap="$(ps -eww -o args= 2>/dev/null | tr ' ' '\n' \
          | grep -m1 -E '^--addons-path=' | sed 's/^--addons-path=//')" || true
    if [ -n "$ap" ]; then printf '%s' "$ap"; return 0; fi
    local conf="$HOME/.config/odoo/odoo.conf"
    if [ -f "$conf" ]; then
        ap="$(grep -E '^[[:space:]]*addons_path[[:space:]]*=' "$conf" \
              | head -n1 | sed -E 's/^[^=]*=[[:space:]]*//')" || true
        if [ -n "$ap" ]; then printf '%s' "$ap"; return 0; fi
    fi
    return 1
}

addon_branch() {  # path -> current branch name (may be empty / "HEAD")
    git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

sanitize_addon_name() {  # path -> [a-z0-9_-] name (<=63)
    local raw="$1" rel
    case "$raw" in
        */src/user/*) rel="${raw##*/src/user/}" ;;
        *) rel="$(basename "$raw")" ;;
    esac
    rel="$(printf '%s' "$rel" | tr '[:upper:]/' '[:lower:]-' | tr -cd 'a-z0-9_-')"
    printf '%s' "${rel:0:63}"
}

upload_addon_files() {  # path name branch category
    local path="$1" name="$2" branch="$3" cat="$4" tries=0
    local base parent
    base="$(basename "$path")"; parent="$(dirname "$path")"
    while :; do
        if tar --exclude='.git' -C "$parent" -cf - "$base" \
            | curl -fsS -H "$AUTH" -H "Content-Type: application/x-tar" \
                   -T - -X POST "${API}/addon?name=${name}&branch=${branch}&category=${cat}" >/dev/null; then
            return 0
        fi
        tries=$((tries + 1))
        [ "$tries" -ge 3 ] && return 1
        sleep 2
    done
}

announce_addon_remote() {  # name origin branch
    curl -fsS -H "$AUTH" -H "Content-Type: application/json" \
        --data-binary "$(printf '{"name":"%s","origin_url":"%s","branch":"%s"}' "$1" "$2" "$3")" \
        -X POST "${API}/addon-remote" >/dev/null
}

process_addons() {
    local addons_path="$1"
    local p base name origin branch
    local IFS=','
    local -a paths
    read -ra paths <<< "$addons_path"
    for p in "${paths[@]}"; do
        p="${p%/}"
        [ -n "$p" ] && [ -d "$p" ] || continue
        case "$p" in
            */src/odoo/addons|*/src/odoo/odoo/addons) continue ;;  # in the image
            */src/user) continue ;;                                # customer's own repo
            */src/enterprise)
                [ "$WITH_ENTERPRISE" = 1 ] || continue
                name="enterprise"; branch="$(addon_branch "$p")"
                if [[ "$done_addons" == *" $name "* ]]; then
                    echo ">> enterprise: already uploaded, skipping"; continue
                fi
                echo ">> enterprise: uploading ($(du -sh "$p" 2>/dev/null | cut -f1))"
                upload_addon_files "$p" "$name" "$branch" "enterprise" \
                    || { echo "ERROR: failed to upload enterprise addons." >&2; exit 5; }
                ;;
            */src/themes)
                [ "$WITH_THEMES" = 1 ] || continue
                name="themes"; branch="$(addon_branch "$p")"
                if [[ "$done_addons" == *" $name "* ]]; then
                    echo ">> themes: already uploaded, skipping"; continue
                fi
                echo ">> themes: uploading ($(du -sh "$p" 2>/dev/null | cut -f1))"
                upload_addon_files "$p" "$name" "$branch" "themes" \
                    || { echo "ERROR: failed to upload themes." >&2; exit 5; }
                ;;
            *)
                [ "$WITH_EXTRA" = 1 ] || continue
                name="$(sanitize_addon_name "$p")"
                [ -n "$name" ] || continue
                origin="$(git -C "$p" remote get-url origin 2>/dev/null || true)"
                branch="$(addon_branch "$p")"
                case "$origin" in
                    https://*)
                        if [[ "$done_remote" == *" $name "* ]]; then
                            echo ">> extra '$name': already announced, skipping"; continue
                        fi
                        echo ">> extra '$name': from remote $origin @ ${branch:-?}"
                        announce_addon_remote "$name" "$origin" "$branch" \
                            || { echo "ERROR: failed to announce extra repo '$name'." >&2; exit 5; }
                        ;;
                    *)
                        if [[ "$done_addons" == *" $name "* ]]; then
                            echo ">> extra '$name': already uploaded, skipping"; continue
                        fi
                        echo ">> extra '$name': uploading files (origin: ${origin:-none})"
                        upload_addon_files "$p" "$name" "$branch" "extra" \
                            || { echo "ERROR: failed to upload extra repo '$name'." >&2; exit 5; }
                        ;;
                esac
                ;;
        esac
    done
}

if [ "$WITH_ENTERPRISE" = 1 ] || [ "$WITH_THEMES" = 1 ] || [ "$WITH_EXTRA" = 1 ]; then
    ADDONS_PATH="$(detect_addons_path || true)"
    if [ -z "$ADDONS_PATH" ]; then
        echo ">> WARNING: could not determine the Odoo addons-path; skipping addon download." >&2
        echo "   (the database and filestore import continues normally)" >&2
    else
        process_addons "$ADDONS_PATH"
    fi
fi

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
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
try:
    d = json.loads(raw)
except ValueError:
    print(">> ERROR: unexpected finalize response (not Oduflow JSON):")
    print("   " + (raw[:300].strip() or "(empty body)"))
    print("   The import may still be finishing server-side — check the")
    print("   Templates tab in the dashboard before retrying.")
    sys.exit(1)
if not d.get("ok"):
    print(">> ERROR:", d.get("error","unknown error")); sys.exit(1)
r=d.get("result",{})
print(">> Done — template ready:", r.get("template_name"))
print("   DB:", r.get("template_db"), " restore:", r.get("restore_seconds"), "s")' "$RESULT_FILE"
