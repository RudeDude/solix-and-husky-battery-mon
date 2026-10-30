#!/usr/bin/env bash
# Print the newest Husky reading, the newest Solix F3800 reading, and the
# usable and remaining capacity from capacity-estimate.env when that file exists.
# Usage: ./status.sh [-v] [data-dir] [estimate-file]
#
# -v also prints the as-of times, zero-watt lines, and the usual
# inactive, not_connected, and AC-output states.

set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
verbose=false
positionals=()
for arg in "$@"; do
  case $arg in
    -v|--verbose) verbose=true ;;
    *) positionals+=("$arg") ;;
  esac
done
data=$root/data
estimate=$root/capacity-estimate.env
if ((${#positionals[@]} >= 1)); then
  data=${positionals[0]}
fi
if ((${#positionals[@]} >= 2)); then
  estimate=${positionals[1]}
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 1
fi

if [[ ! -d $data ]]; then
  echo "no log directory at $data" >&2
  exit 1
fi

mapfile -d '' files < <(find "$data" -type f -name 'readings.ndjson' -print0 | sort -zr)
if ((${#files[@]} == 0)); then
  echo "no readings under $data" >&2
  exit 1
fi

# Newest valid sample for one device. Error lines and a half-written last
# line are skipped. Day folders sort newest first because they are YYYY/MM/DD.
latest() {
  local device=$1 file line
  for file in "${files[@]}"; do
    line=$(
      set +o pipefail
      tac "$file" | jq -R -c --arg device "$device" '
        fromjson?
        | select(type == "object" and .device == $device and (has("error") | not))
      ' | head -n 1
    )
    if [[ -n $line ]]; then
      printf '%s\n' "$line"
      return 0
    fi
  done
  return 1
}

print_husky() {
  jq -r --argjson verbose "$verbose" '
    def num(v; digits):
      if v == null then empty else (v | tonumber | . * pow(10; digits) | round / pow(10; digits) | tostring) end;
    "Husky 2   \(.name // "BB-GOLF-BT1128")",
    (if $verbose then "  as of            \(.time)" else empty end),
    (if .soc_percent != null then "  state of charge  \(num(.soc_percent; 1))%" else empty end),
    (if .voltage_v != null then "  voltage          \(num(.voltage_v; 2)) V" else empty end),
    (if .current_a != null then
      "  current          \(if .current_a > 0 then "+" else "" end)\(num(.current_a; 2)) A  \(.charge_state // "")"
    else empty end),
    (if .min_cell_v != null and .max_cell_v != null then
      "  cells            \((.cells_v // []) | length)   \(num(.min_cell_v; 3))–\(num(.max_cell_v; 3)) V"
      + (if .delta_cell_v != null then "   delta \(num(.delta_cell_v * 1000; 0)) mV" else "" end)
    else empty end),
    (if (.temperatures_c // []) | length > 0 then
      "  temperature      \([.temperatures_c[] | "\(.name) \(num(.celsius; 1)) C"] | join(", "))"
    else empty end),
    (if (.notes // []) | length > 0 then "  notes            \(.notes | join("; "))" else empty end)
  '
}

print_solix() {
  jq -r --argjson verbose "$verbose" '
    def num(v; digits):
      if v == null then empty else (v | tonumber | . * pow(10; digits) | round / pow(10; digits) | tostring) end;
    def watts(key; title):
      if has(key) and .[key] != null and ($verbose or ((.[key] | tonumber | round) != 0)) then
        "  \(title) \(num(.[key]; 0)) W"
      else empty end;
    def state(title; value; usual):
      if value == null then empty
      elif $verbose or value != usual then "  \(title)\(value)"
      else empty end;
    "Solix F3800   \(.name // "F3800")",
    (if $verbose then "  as of            \(.time)" else empty end),
    (if .soc_percent != null then "  state of charge  \(num(.soc_percent; 1))%" else empty end),
    (if .temperature_c != null then "  temperature      \(num(.temperature_c; 1)) C" else empty end),
    watts("ac_power_in_w"; "AC in           "),
    watts("ac_power_out_w"; "AC out          "),
    watts("output_power_w"; "output          "),
    watts("charge_power_w"; "charging power  "),
    watts("solar_power_w"; "solar           "),
    watts("solar_pv1_w"; "solar port 1    "),
    watts("solar_pv2_w"; "solar port 2    "),
    watts("discharge_power_w"; "discharge       "),
    (
      . as $row
      | ["usb_c1_w","usb_c2_w","usb_c3_w","usb_a1_w","usb_a2_w"] as $ports
      | [$ports[] | select($row[.] != null and $row[.] != 0) | "\(.) \($row[.] | tonumber | round) W"] as $live
      | if $live | length > 0 then "  usb              \($live | join(", "))" else empty end
    ),
    state("charging         "; .charging; "inactive"),
    state("AC output        "; .ac_output; "output"),
    state("DC output        "; .dc_output; "not_connected"),
    (if .hours_remaining != null then "  hours remaining  \(num(.hours_remaining; 1)) h" else empty end)
  '
}

print_capacity() {
  local label pct wh value i w_name h_name w h usable
  echo
  echo "Capacity"
  usable=${SHARED_USABLE_WH:-}
  if [[ -n $usable ]]; then
    printf '  %-17s%s\n' "usable" "$usable Wh"
  fi
  pct=${SHARED_REMAINING_PERCENT:-}
  wh=${SHARED_REMAINING_WH:-}
  if [[ -n $pct || -n $wh ]]; then
    value=""
    if [[ -n $pct ]]; then
      value="${pct}%"
    fi
    if [[ -n $wh ]]; then
      value="${value:+$value   }${wh} Wh"
    fi
    printf '  %-17s%s\n' "remaining" "$value"
  fi
  if [[ ${RUNTIME_COUNT:-0} -gt 0 ]]; then
    label="runtime"
    for ((i = 1; i <= RUNTIME_COUNT; i++)); do
      w_name=RUNTIME_${i}_W
      h_name=RUNTIME_${i}_REMAINING_H
      w=${!w_name:-}
      h=${!h_name:-}
      if [[ -z $w || -z $h ]]; then
        continue
      fi
      printf '  %-17s%6s W %6s h\n' "$label" "$w" "$h"
      label=""
    done
  fi
}

found=0
if husky=$(latest husky); then
  printf '%s\n' "$husky" | print_husky
  found=1
else
  echo "Husky 2   no reading under $data"
fi

echo
if solix=$(latest f3800); then
  printf '%s\n' "$solix" | print_solix
  found=1
else
  echo "Solix F3800   no reading under $data"
fi

shown_estimate=0
if [[ -f $estimate ]]; then
  # The file is assignments written by python -m capacity.
  # shellcheck disable=SC1090
  source "$estimate"
  print_capacity
  shown_estimate=1
else
  echo
  echo "Capacity"
  echo "  no estimate yet. Run python -m capacity."
fi

if ((found == 0 && shown_estimate == 0)); then
  exit 1
fi
