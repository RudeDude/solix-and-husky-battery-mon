#!/usr/bin/env bash
# Print the newest Husky reading and the newest Solix F3800 reading.
# Usage: ./status.sh [data-dir]

set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
data=${1:-"$root/data"}

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
  jq -r '
    def num(v; digits):
      if v == null then empty else (v | tonumber | . * pow(10; digits) | round / pow(10; digits) | tostring) end;
    "Husky 2   \(.name // "BB-GOLF-BT1128")",
    "  as of            \(.time)",
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
  jq -r '
    def num(v; digits):
      if v == null then empty else (v | tonumber | . * pow(10; digits) | round / pow(10; digits) | tostring) end;
    def watts(key; title):
      if has(key) then "  \(title) \(num(.[key]; 0)) W" else empty end;
    "Solix F3800   \(.name // "F3800")",
    "  as of            \(.time)",
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
    (if .charging != null then "  charging         \(.charging)" else empty end),
    (if .ac_output != null then "  AC output        \(.ac_output)" else empty end),
    (if .dc_output != null then "  DC output        \(.dc_output)" else empty end),
    (if .hours_remaining != null then "  hours remaining  \(num(.hours_remaining; 1)) h" else empty end)
  '
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

if ((found == 0)); then
  exit 1
fi
