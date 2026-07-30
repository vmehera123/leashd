#!/bin/bash
# Template: Batched search over one browser
# Purpose: Run many search queries through staggered tabs and print ranked results
# Usage: ./search-batch.sh [-n inflight] [-j min:max] [-e engine] [-c count] "query" ...
#        ./search-batch.sh [options] -f queries.txt
#
# Google serves a /sorry/index interstitial when several result pages are
# requested at once. Measured on leashd's persistent profile: 15 tabs opened
# back-to-back lost 2 pages, the same 15 queries opened one at a time lost none.
# This script keeps a small number of tabs in flight and waits a random gap
# between opens, then falls back to another engine for any query Google still
# refuses.
#
# Output, per query:
#   ## <query>   [engine]
#   <title><TAB><url>

set -uo pipefail

INFLIGHT=4
JITTER_MIN=2
JITTER_MAX=5
ENGINE=google
COUNT=10
QUERY_FILE=
EXTRACT_ATTEMPTS=8
EXTRACT_POLL_SECONDS=1.2

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while getopts ":n:j:e:c:f:h" opt; do
    case "$opt" in
        n) INFLIGHT="$OPTARG" ;;
        j) JITTER_MIN="${OPTARG%%:*}"; JITTER_MAX="${OPTARG##*:}" ;;
        e) ENGINE="$OPTARG" ;;
        c) COUNT="$OPTARG" ;;
        f) QUERY_FILE="$OPTARG" ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done
shift $((OPTIND - 1))

QUERIES=()
if [[ -n "$QUERY_FILE" ]]; then
    while read -r line; do
        [[ -n "$line" ]] && QUERIES+=("$line")
    done < "$QUERY_FILE"
fi
QUERIES+=("$@")
[[ ${#QUERIES[@]} -eq 0 ]] && usage 1

engine_url() {
    local engine="$1" encoded="$2"
    case "$engine" in
        google) printf 'https://www.google.com/search?q=%s&num=20' "$encoded" ;;
        duckduckgo) printf 'https://duckduckgo.com/?q=%s' "$encoded" ;;
        bing) printf 'https://www.bing.com/search?q=%s' "$encoded" ;;
        *) return 1 ;;
    esac
}

engine_selector() {
    case "$1" in
        google) printf 'a:has(h3)' ;;
        duckduckgo) printf 'a[data-testid="result-title-a"]' ;;
        bing) printf '#b_results h2 a' ;;
    esac
}

fallback_engine() {
    case "$1" in
        google) printf 'duckduckgo' ;;
        duckduckgo) printf 'bing' ;;
        *) printf '' ;;
    esac
}

urlencode() {
    python3 -c 'import sys,urllib.parse;print(urllib.parse.quote_plus(sys.argv[1]))' "$1"
}

jitter() {
    awk -v a="$JITTER_MIN" -v b="$JITTER_MAX" -v r="$RANDOM" \
        'BEGIN { printf "%.1f", a + (r / 32767) * (b - a) }'
}

blocked_url() {
    case "$1" in
        *"/sorry/"* | *"/anomaly"* | *"captcha"* | *"consent."*) return 0 ;;
        *) return 1 ;;
    esac
}

extract_once() {
    local selector="$1"
    agent-browser eval --json "JSON.stringify([...document.querySelectorAll('${selector}')].slice(0, ${COUNT}).map(a => ({ t: (a.querySelector('h3') || a).textContent.trim(), u: a.href })))" 2>/dev/null |
        python3 -c '
import base64, json, sys, urllib.parse


def unwrap(url):
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    if "bing.com" in parsed.netloc and params.get("u", [""])[0].startswith("a1"):
        raw = params["u"][0][2:]
        padded = raw + "=" * (-len(raw) % 4)
        try:
            return base64.urlsafe_b64decode(padded).decode()
        except Exception:
            return url
    if "duckduckgo.com" in parsed.netloc and params.get("uddg"):
        return params["uddg"][0]
    return url


try:
    payload = json.load(sys.stdin)["data"]
    rows = json.loads(payload.get("result") or "[]")
except Exception:
    sys.exit(1)
for row in rows:
    url = row.get("u")
    if url:
        print(row.get("t", "") + "\t" + unwrap(url))
'
}

extract() {
    local selector="$1" label="$2" rows attempt
    for (( attempt = 0; attempt < EXTRACT_ATTEMPTS; attempt++ )); do
        agent-browser tab "$label" >/dev/null 2>&1
        rows="$(extract_once "$selector")"
        if [[ -n "$rows" ]]; then
            printf '%s\n' "$rows"
            return 0
        fi
        sleep "$EXTRACT_POLL_SECONDS"
    done
    return 1
}

read_tab() {
    local engine="$1" label="$2" query="$3" encoded="$4"
    local started="$1" url rows next backoff

    agent-browser tab "$label" >/dev/null 2>&1
    while :; do
        agent-browser wait --load domcontentloaded >/dev/null 2>&1
        url="$(agent-browser get url 2>/dev/null)"

        if blocked_url "$url"; then
            backoff="$(awk -v j="$(jitter)" 'BEGIN { printf "%.1f", j * 3 }')"
            printf '%s: %s served an interstitial, backing off %ss\n' \
                "$query" "$engine" "$backoff" >&2
            sleep "$backoff"
            agent-browser open "$(engine_url "$engine" "$encoded")" >/dev/null 2>&1
            agent-browser wait --load domcontentloaded >/dev/null 2>&1
            url="$(agent-browser get url 2>/dev/null)"
        fi

        if ! blocked_url "$url"; then
            rows="$(extract "$(engine_selector "$engine")" "$label")"
            if [[ -n "$rows" ]]; then
                printf '## %s\t[%s]\n%s\n\n' "$query" "$engine" "$rows"
                [[ "$engine" != "$started" ]] && FALLBACK_ENGINE="$engine"
                return 0
            fi
        fi

        next="$(fallback_engine "$engine")"
        if [[ -z "$next" ]]; then
            printf '%s: no results from any engine\n' "$query" >&2
            printf '## %s\t[none]\n\n' "$query"
            return 1
        fi
        printf '%s: %s yielded nothing, trying %s\n' "$query" "$engine" "$next" >&2
        engine="$next"
        agent-browser open "$(engine_url "$engine" "$encoded")" >/dev/null 2>&1
    done
}

if ! engine_url "$ENGINE" test >/dev/null; then
    printf 'unknown engine: %s (google|duckduckgo|bing)\n' "$ENGINE" >&2
    exit 1
fi

FALLBACK_ENGINE=
total=${#QUERIES[@]}
index=0

while (( index < total )); do
    labels=()
    batch=()
    encodings=()

    for (( slot = 0; slot < INFLIGHT && index < total; slot++, index++ )); do
        query="${QUERIES[index]}"
        encoded="$(urlencode "$query")"
        label="sb$index"
        agent-browser tab new --label "$label" \
            "$(engine_url "${FALLBACK_ENGINE:-$ENGINE}" "$encoded")" >/dev/null 2>&1
        labels+=("$label")
        batch+=("$query")
        encodings+=("$encoded")
        if (( slot + 1 < INFLIGHT && index + 1 < total )); then
            sleep "$(jitter)"
        fi
    done

    for (( i = 0; i < ${#labels[@]}; i++ )); do
        read_tab "${FALLBACK_ENGINE:-$ENGINE}" "${labels[i]}" "${batch[i]}" "${encodings[i]}"
        agent-browser tab close "${labels[i]}" >/dev/null 2>&1
    done
done
