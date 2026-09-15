#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# e2e.sh — end-to-end API check for Vaultly.
#
# Drives the real HTTP API the way a browser does, from two INDEPENDENT cookie
# jars, which is what proves "an account created on one device can be logged
# into from another".
#
#   ./tests/e2e.sh                      # against http://127.0.0.1:8000
#   BASE=https://host ./tests/e2e.sh    # against a deployed instance
# ---------------------------------------------------------------------------
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
TMP="$(mktemp -d)"
A="$TMP/deviceA.jar"   # device A: signs up
B="$TMP/deviceB.jar"   # device B: a *separate* session - logs in
EMAIL="e2e.$(date +%s%N | tail -c 6).$(shuf -i 1000-9999 -n1)@example.com"
PW="e2e-pass-7723"
PASS=0
FAIL=0

c()    { curl -s --max-time 20 "$@"; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }

# ok <label> <expected> <actual>
ok() {
  if [ "$2" = "$3" ]; then
    printf '  \033[32mPASS\033[0m  %-46s %s\n' "$1" "$3"; PASS=$((PASS+1))
  else
    printf '  \033[31mFAIL\033[0m  %-46s got %s want %s\n' "$1" "$3" "$2"; FAIL=$((FAIL+1))
  fi
}

# has <label> <needle> <haystack>
has() {
  case "$3" in
    *"$2"*) printf '  \033[32mPASS\033[0m  %-46s %s\n' "$1" "\"$2\""; PASS=$((PASS+1)) ;;
    *)      printf '  \033[31mFAIL\033[0m  %-46s missing: %s\n' "$1" "$2"; FAIL=$((FAIL+1)) ;;
  esac
}

# absent <label> <needle> <haystack> - the string must NOT appear
absent() {
  case "$3" in
    *"$2"*) printf '  \033[31mFAIL\033[0m  %-46s still present: %s\n' "$1" "$2"; FAIL=$((FAIL+1)) ;;
    *)      printf '  \033[32mPASS\033[0m  %-46s gone\n' "$1"; PASS=$((PASS+1)) ;;
  esac
}

echo "Vaultly end-to-end  ->  $BASE"
echo "test account: $EMAIL"
echo

echo "1. service health"
ok "GET /api/health" 200 "$(code "$BASE/api/health")"

echo
echo "2. validation (server-side)"
BODY="$(c -X POST "$BASE/api/signup" -H 'Content-Type: application/json' \
        -d '{"email":"not-an-email","password":"e2e-pass-7723","confirm":"e2e-pass-7723"}')"
has "invalid email rejected" "does not look right" "$BODY"

BODY="$(c -X POST "$BASE/api/signup" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"abc\",\"confirm\":\"abc\"}")"
has "password < 5 chars rejected" "at least 5 characters" "$BODY"

BODY="$(c -X POST "$BASE/api/signup" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\",\"confirm\":\"different-9\"}")"
has "password mismatch rejected" "do not match" "$BODY"

echo
echo "3. sign up on DEVICE A"
ok "POST /api/signup" 201 "$(code -c "$A" -X POST "$BASE/api/signup" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\",\"confirm\":\"$PW\",\"name\":\"E2E Tester\"}")"
ok "GET  /api/session (device A)" 200 "$(code -b "$A" "$BASE/api/session")"
has "session returns the signed-in user" "$EMAIL" "$(c -b "$A" "$BASE/api/session")"

echo
echo "4. duplicate account"
BODY="$(c -X POST "$BASE/api/signup" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\",\"confirm\":\"$PW\"}")"
ok "duplicate -> 409" 409 "$(code -X POST "$BASE/api/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\",\"confirm\":\"$PW\"}")"
has "duplicate message" "already registered" "$BODY"

echo
echo "5. cross-device login from DEVICE B (separate cookie jar)"
BODY="$(c -X POST "$BASE/api/login" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"wrong-password\"}")"
has "wrong password rejected" "Incorrect password" "$BODY"

ok "POST /api/login (device B)" 200 "$(code -c "$B" -X POST "$BASE/api/login" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}")"
has "device B sees the same account" "$EMAIL" "$(c -b "$B" "$BASE/api/session")"

TOKA="$(grep -o 'wf_session[[:space:]]*[^ ]*$' "$A" | awk '{print $NF}' | tail -1)"
TOKB="$(grep -o 'wf_session[[:space:]]*[^ ]*$' "$B" | awk '{print $NF}' | tail -1)"
if [ -n "$TOKA" ] && [ -n "$TOKB" ] && [ "$TOKA" != "$TOKB" ]; then
  printf '  \033[32mPASS\033[0m  %-46s\n' "device A and B hold DIFFERENT session tokens"
  PASS=$((PASS+1))
else
  printf '  \033[31mFAIL\033[0m  %-46s\n' "device A and B should not share a token"
  FAIL=$((FAIL+1))
fi

echo
echo "6. withdrawal flow (device B)"
ok "GET  /api/methods" 200 "$(code -b "$B" "$BASE/api/methods")"
QUOTE="$(c -b "$B" -X POST "$BASE/api/withdrawal/quote" -H 'Content-Type: application/json' -d '{"amount":250}')"
has "quote: amount"       '"amount": "$250.00"' "$QUOTE"
has "quote: fee 1% min"   '"fee": "$2.50"'    "$QUOTE"
has "quote: net"          '"net": "$247.50"'   "$QUOTE"
has "quote: fee in cents" '"feeCents": 250'    "$QUOTE"

NEW="$(c -b "$B" -X POST "$BASE/api/withdrawal" -H 'Content-Type: application/json' \
       -d '{"amount":250,"method":"paypal","destination":"e2e@example.com"}')"
has "withdrawal created"  '"status": "pending"'   "$NEW"
has "balance debited"      '"funds": "$99,750.00"' "$NEW"
has "reference issued"     "WD-"                  "$NEW"

BODY="$(c -b "$B" -X POST "$BASE/api/withdrawal" -H 'Content-Type: application/json' \
        -d '{"amount":999999,"method":"paypal","destination":"e2e@example.com"}')"
has "above limit rejected" "more than your available" "$BODY"

BODY="$(c -b "$B" -X POST "$BASE/api/withdrawal" -H 'Content-Type: application/json' \
        -d '{"amount":2,"method":"paypal","destination":"e2e@example.com"}')"
has "below minimum rejected" "smallest withdrawal" "$BODY"

HIST="$(c -b "$B" "$BASE/api/withdrawal/history")"
has "history contains the withdrawal" "WD-" "$HIST"

echo
echo "7. unauthenticated access is refused"
ok "GET /api/withdrawal/history without a session -> 401" 401 \
   "$(code "$BASE/api/withdrawal/history")"

echo
echo "8. logout (device B), then a THIRD fresh session proves persistence"
ok "POST /api/logout" 200 "$(code -b "$B" -c "$B" -X POST "$BASE/api/logout")"
has "device B session is gone" '"user": null' "$(c -b "$B" "$BASE/api/session")"

C="$TMP/deviceC.jar"
ok "device C logs in to the same account" 200 "$(code -c "$C" -X POST "$BASE/api/login" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}")"
has "device C reads the withdrawal history" "WD-" "$(c -b "$C" "$BASE/api/withdrawal/history")"

echo
echo "9. static assets and single-screen layout"
ok "GET /styles.css"  200 "$(code "$BASE/styles.css")"
ok "GET /app.js"      200 "$(code "$BASE/app.js")"
ok "GET /icons.js"    200 "$(code "$BASE/icons.js")"
ok "GET /assets/hero-vault.jpg" 200 "$(code "$BASE/assets/hero-vault.jpg")"

HTML="$(c "$BASE/")"
has "hero illustration referenced"   "assets/hero-vault.jpg" "$HTML"
has "one-screen-at-a-time stepper"   "Withdrawal progress"   "$HTML"
absent "no demo banner left behind"  "Demo / simulation only" "$HTML"
absent "no demo notices left behind" "this is not a real financial product" "$HTML"

rm -rf "$TMP"

echo
echo "-----------------------------------------------"
printf 'passed: %s   failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL CHECKS PASSED" || echo "THERE ARE FAILURES"
exit "$FAIL"
