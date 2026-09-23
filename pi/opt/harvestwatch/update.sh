#!/bin/bash
# HarvestWatch Pi auto-updater
# ------------------------------------------------------------------
# Runs every 5 minutes (harvestwatch-update.timer). If GitHub has a new
# commit, copies the changed files under pi/ to the same path on the Pi,
# restarts only the services that use them, and checks they stay up.
# If a restarted service fails, it puts the old files back, restarts
# again, and sends an ntfy alert. Offline = does nothing, tries later.
#
#   sudo /opt/harvestwatch/update.sh           normal run (what the timer does)
#   sudo /opt/harvestwatch/update.sh --check   list repo files that differ from the Pi
#   journalctl -u harvestwatch-update -n 50    see what it did
# ------------------------------------------------------------------
set -uo pipefail
REPO=/home/harvestmoon/HarvestWatch
BRANCH=main
OWNER=harvestmoon
STATE=/var/lib/harvestwatch
SELF_UNITS="harvestwatch-update.service harvestwatch-update.timer"

mkdir -p "$STATE"
exec 9>"$STATE/lock"; flock -n 9 || exit 0

G() { sudo -u "$OWNER" git -C "$REPO" "$@"; }
log() { echo "$*"; }
notify() {  # title, message - uses the same ntfy settings as guard
  [ -r /etc/harvest-moon/ntfy.env ] || return 0
  ( set -a; . /etc/harvest-moon/ntfy.env; set +a
    [ -n "${NTFY_TOPIC:-}" ] || exit 0
    auth=(); [ -n "${NTFY_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $NTFY_TOKEN")
    curl -fsS -m 20 "${auth[@]}" -H "Title: $1" -H "Tags: computer" -d "$2" \
      "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null ) || log "ntfy send failed"
}
deployable() {  # repo path -> should it be copied to the Pi?
  case "$1" in pi/*.example|pi/*/.DS_Store) return 1;; pi/*) return 0;; *) return 1;; esac
}
dest_of() { echo "/${1#pi/}"; }

# ---------- --check: compare repo with what is on the Pi ----------
if [ "${1:-}" = "--check" ]; then
  G fetch -q origin "$BRANCH" && G reset -q --hard "origin/$BRANCH"
  G ls-files pi | while read -r f; do
    deployable "$f" || continue
    d=$(dest_of "$f")
    if [ ! -e "$d" ]; then echo "MISSING on Pi  $d"
    elif cmp -s "$REPO/$f" "$d"; then echo "same           $d"
    else echo "DIFFERENT      $d"; fi
  done
  exit 0
fi

# ---------- normal run ----------
G fetch -q origin "$BRANCH" || { log "fetch failed (offline?) - will retry"; exit 0; }
NEW=$(G rev-parse "origin/$BRANCH")
OLD=$(cat "$STATE/deployed" 2>/dev/null || true)
if [ -z "$OLD" ]; then
  G reset -q --hard "$NEW"; echo "$NEW" > "$STATE/deployed"
  log "first run: baseline set to ${NEW:0:7}, nothing copied"; exit 0
fi
[ "$OLD" = "$NEW" ] && exit 0
[ "$(cat "$STATE/failed" 2>/dev/null)" = "$NEW" ] && exit 0   # already rolled this one back

G reset -q --hard "$NEW"
SUBJECT=$(G log -1 --format=%s "$NEW")
BK="$STATE/backup/${NEW:0:7}"; mkdir -p "$BK"
changed=(); units_changed=0; restart=(); newunits=()

while read -r f; do
  deployable "$f" || continue
  [ -e "$REPO/$f" ] || { log "removed in repo, left on Pi: $(dest_of "$f")"; continue; }
  d=$(dest_of "$f")
  [ -e "$d" ] && cmp -s "$REPO/$f" "$d" && continue
  if [ -e "$d" ]; then mkdir -p "$BK$(dirname "$d")"; cp -p "$d" "$BK$d"; else echo "$d" >> "$BK/.new"; fi
  mode=644; [ -x "$REPO/$f" ] && mode=755
  own=root; case "$d" in /home/$OWNER/*) own=$OWNER;; esac
  install -D -o "$own" -g "$own" -m "$mode" "$REPO/$f" "$d"
  changed+=("$d"); log "updated $d"
  case "$d" in
    /etc/systemd/system/*)
      units_changed=1; u=$(basename "$d")
      [[ " $SELF_UNITS " == *" $u "* ]] && continue
      [ -e "$BK$d" ] || newunits+=("$u")
      [[ "$u" == *.service ]] && restart+=("${u%.service}") ;;
    *.md|*.txt) ;;  # notes - no restart needed
    *)  # which services use this file? exact path first, then its folder
      m=$(grep -lF "$d" /etc/systemd/system/*.service 2>/dev/null)
      dir=$(dirname "$d")
      [ -z "$m" ] && [ "$dir" != "/home/$OWNER" ] && m=$(grep -lF "$dir/" /etc/systemd/system/*.service 2>/dev/null)
      [ -z "$m" ] && [ "$dir" != "/home/$OWNER" ] && m=$(grep -lE "WorkingDirectory=$dir\$" /etc/systemd/system/*.service 2>/dev/null)
      for s in $m; do s=$(basename "$s" .service)
        [[ " $SELF_UNITS " == *" $s.service "* ]] || restart+=("$s"); done ;;
  esac
done < <(G diff --name-only "$OLD" "$NEW" -- pi/)

[ "$units_changed" = 1 ] && systemctl daemon-reload
for u in "${newunits[@]}"; do systemctl enable "$u" && log "enabled new unit $u"; done
mapfile -t restart < <(printf '%s\n' "${restart[@]}" | sort -u | sed '/^$/d')

if [ ${#changed[@]} -eq 0 ]; then echo "$NEW" > "$STATE/deployed"; log "${NEW:0:7}: nothing for the Pi"; exit 0; fi

declare -A t0
for s in "${restart[@]}"; do systemctl restart "$s"; done
sleep 3
for s in "${restart[@]}"; do t0[$s]=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$s"); done
sleep 30
bad=()
for s in "${restart[@]}"; do
  if ! systemctl is-active -q "$s" || [ "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$s")" != "${t0[$s]}" ]; then
    bad+=("$s"); fi
done

if [ ${#bad[@]} -eq 0 ]; then
  echo "$NEW" > "$STATE/deployed"
  msg="${NEW:0:7} \"$SUBJECT\" - ${#changed[@]} file(s)"; [ ${#restart[@]} -gt 0 ] && msg="$msg; restarted: ${restart[*]}"
  log "OK: $msg"; notify "Pi updated" "$msg"
else
  log "FAILED: ${bad[*]} - rolling back"
  for d in "${changed[@]}"; do [ -e "$BK$d" ] && cp -p "$BK$d" "$d"; done
  [ "$units_changed" = 1 ] && systemctl daemon-reload
  for s in "${restart[@]}"; do systemctl restart "$s"; done
  echo "$NEW" > "$STATE/failed"
  notify "Pi update ROLLED BACK" "${NEW:0:7} \"$SUBJECT\": ${bad[*]} would not stay running. Old files restored. Check: journalctl -u ${bad[0]} -n 50"
  exit 1
fi
