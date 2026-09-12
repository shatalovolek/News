#!/bin/sh
# Check the news subdomain from DNS to a working page.
#   ./scripts_check_domain.sh news.beat-trade.com
set -eu
HOST=${1:-news.beat-trade.com}
echo "== DNS =="
CN=$(dig +short CNAME "$HOST" | head -1); A=$(dig +short A "$HOST" | head -1)
[ -n "$CN" ] && echo "  CNAME -> $CN" || echo "  no CNAME record"
[ -n "$A" ] && echo "  A     -> $A" || echo "  no A record"
[ -z "$CN" ] && [ -z "$A" ] && { echo "  record has not propagated yet, wait and retry"; exit 1; }
echo "== TLS and page =="
curl -s -o /tmp/newsroom.html -w "  GET / -> %{http_code}, TLS %{ssl_verify_result} (0 = certificate ok)\n" --max-time 90 "https://$HOST/"
grep -q "Newsroom" /tmp/newsroom.html && echo "  page contains the Newsroom" || echo "  page did not look like the Newsroom"
echo "== API =="
curl -s --max-time 60 "https://$HOST/api/health"; echo
