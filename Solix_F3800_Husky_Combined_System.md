# Combined Anker Solix F3800 + BigBattery Husky V2 48V System  
## Electrical Properties, Voltage Behavior, Capacity, and Estimation

**Document purpose**: Describe the electrical characteristics of paralleling an Anker Solix F3800 portable power station with a BigBattery Husky V2 48V (model FHSKY-48051-G2) via the Solix expansion battery bus, as demonstrated in NoiseLab YouTube content. Focus on voltage behavior (cell and pack), total capacity, and practical methods to estimate remaining capacity during discharge.  

**Key observation from community testing (NoiseLab et al.)**: When the Solix is powered on, the expansion battery port provides direct access to the internal battery bus. Connecting a compatible 51.2 V LiFePO4 pack in parallel works, and the Solix’s internal metering (including its displayed capacity percentage and input/output wattage) accounts for net energy flow through the bus. The displayed % remains useful as an approximation of overall system state of charge (SOC) when the packs are reasonably matched.

---

## 1. Individual Battery Specifications

### Anker Solix F3800
- **Chemistry**: LiFePO4 (LFP)
- **Nominal voltage**: 51.2 V
- **Capacity**: 3,840 Wh (75 Ah at 51.2 V)
- **Internal configuration**: 16S5P (80 cells of ~15 Ah each; Cell Part Number 32140FS-15000mAh)
- **Typical operating voltage range**: ~48–57.6 V (standard 16S LFP; exact BMS cutoffs are internal and not fully published)
- **User-visible data**: Capacity percentage, input/output wattage (via screen/app). No direct cell or pack voltage readout.

### BigBattery Husky V2 48V (FHSKY-48051-G2)
- **Chemistry**: LiFePO4
- **Nominal voltage**: 51.2 V
- **Capacity**: 5.12 kWh (100 Ah)
- **Cell configuration**: 16S
- **Operating voltage range**: 48 V – 57 V
- **Charging voltage range**: 55.6 V – 57 V
- **Max charge voltage**: 58.8 V (some variants list 57.6 V)
- **Suggested low-voltage cutoff**: 48 V – 50.8 V
- **BMS cutoff range**: 42 V – 47 V
- **Continuous discharge**: 100 A (recommended); peak higher for short durations
- **Monitoring**: Full cell and pack voltage data via Bluetooth app (plus SOC, currents, temperatures, etc.)

Both packs are electrically compatible 16-series LiFePO4 systems with identical nominal voltage.

---

## 2. Combined System Electrical Properties

When the Husky is connected in parallel on the Solix battery bus:

- **Topology**: True parallel connection on the DC bus. Both packs share the same terminal voltage (minus small IR drops in cabling and connectors).
- **Voltage**: The system voltage is the common pack voltage of the paralleled banks. It follows a standard 16S LiFePO4 curve.
- **Current sharing**: Approximately proportional to capacity (roughly 43% Solix / 57% Husky at the Ah level) but influenced by differences in internal resistance, cable resistance, temperature, and instantaneous SOC. Current can flow between the packs to equalize them.
- **Energy**: Additive. Nominal total capacity ≈ **8,960 Wh** (3,840 + 5,120). Effective usable capacity is typically 90–95%+ of nominal for LFP under moderate loads, subject to the lower of the two packs’ protective cutoffs and any efficiency losses in the Solix inverter/charger.
- **Power capability**: Limited by the Solix inverter/charger (6 kW continuous AC output, ~1.8 kW AC charge, up to 2.4 kW solar) and by the bus cabling/fusing. The Husky can contribute substantial current; ensure interconnects are sized appropriately (high-current cabling and proper fusing/breakers are essential).
- **Balancing**: The packs will tend to equalize over time through the parallel connection. Best practice is to connect them when their individual SOCs are close to avoid large equalization currents.
- **Solix metering behavior**: Community testing indicates the Solix coulomb-counts net energy in and out of the entire system (including the expansion port). The displayed percentage therefore approximates the combined SOC when the packs are well matched. Wattage readings reflect system input/output.

---

## 3. Expected Cell and Pack Voltage Behavior

Both packs are 16S LiFePO4. Pack voltage = 16 × average cell voltage (assuming cells are balanced).

### Approximate Resting (Open-Circuit) Voltage vs. SOC for 16S LiFePO4
These are typical values after a rest period (30+ minutes with no significant charge or discharge). Actual values vary slightly by cell manufacturer, temperature, age, and history. The curve is characteristically flat in the middle range.

| Approx. SOC | Cell Voltage (avg) | Pack Voltage (16S) | Notes |
|-------------|--------------------|--------------------|-------|
| 100% (rested) | ~3.40 V | ~54.4 V | After charge taper and rest |
| 90% | ~3.35 V | ~53.6 V | Top of plateau |
| 80% | ~3.32–3.33 V | ~53.1–53.3 V | |
| 70% | ~3.30 V | ~52.8 V | |
| 50–60% | ~3.26–3.28 V | ~52.2–52.5 V | Very flat region |
| 40% | ~3.25 V | ~52.0 V | |
| 30% | ~3.22–3.25 V | ~51.5–52.0 V | |
| 20% | ~3.20–3.22 V | ~51.2–51.5 V | Voltage begins to fall more noticeably |
| 10% | ~3.00–3.10 V | ~48–49.6 V | Steep drop begins |
| Practical empty | ~2.9–3.0 V | ~46.4–48 V | Depends on BMS settings |
| BMS hard cutoff | ~2.5 V | ~40 V | Avoid reaching this |

**During charge**:
- Voltage rises according to charge current and SOC.
- Absorption typically targets ~55.6–57.6 V (3.475–3.6 V/cell). Some systems briefly allow higher.
- At high SOC the voltage rises more steeply; current tapers.
- The Husky Bluetooth data will show individual cell voltages climbing together if balanced.

**During discharge**:
- Voltage sags under load (IR drop) relative to the resting curve.
- In the flat mid-range (roughly 20–90% SOC) voltage changes very little, making pure voltage-based SOC estimation imprecise under load.
- Near the bottom the voltage falls more rapidly.
- The Solix will continue operating until its internal low-voltage cutoff is reached. The Husky BMS will protect its own cells independently.

Because the packs are paralleled, the Husky cell/pack voltages are an excellent real-time window into the common bus voltage experienced by both batteries.

---

## 4. Expected Total Capacity

| Parameter | Solix F3800 | Husky V2 | Combined |
|-----------|-------------|----------|----------|
| Nominal energy | 3,840 Wh | 5,120 Wh | **8,960 Wh** |
| Nominal Ah (@ 51.2 V) | 75 Ah | 100 Ah | **175 Ah** |
| Typical usable (est.) | ~3,500–3,700 Wh | ~4,800–5,000 Wh | **~8,300–8,700 Wh** |

Usable capacity depends on:
- Depth of discharge allowed by the controlling BMS (Solix internal + Husky BMS)
- Load current (higher current reduces effective capacity slightly)
- Temperature
- Age/cycle history of each pack
- Efficiency of the Solix inverter (AC loads) or charger

The Solix percentage display, when the packs are matched, provides a practical real-time indicator of remaining combined capacity.

---

## 5. Estimating Combined System Capacity During Discharge

You have two complementary data sources:

1. **Solix**: Capacity % and instantaneous input/output wattage.
2. **Husky (Bluetooth)**: Precise pack voltage and individual cell voltages.

### Practical Estimation Methods

**Method A – Solix percentage (simplest, usually good enough)**  
If the packs were connected at similar SOC and have been cycling together, the Solix displayed percentage is a reasonable proxy for system SOC.  

\[
\text{Remaining energy (Wh)} \approx \left( \frac{\text{Solix \%}}{100} \right) \times 8960
\]

This works because the Solix is coulomb-counting net system energy.

**Method B – Voltage-based (cross-check / when percentage is questionable)**  
Use the Husky pack voltage (or average cell voltage × 16) and the table above to estimate SOC, then apply the total capacity.  
- Most accurate after a rest period.  
- Under load, expect some voltage sag; higher loads produce more sag.  
- Mid-range voltage is relatively insensitive to SOC, so this method is coarse between ~20–80%.

**Method C – Energy integration (most accurate for a discharge event)**  
1. Note the Solix percentage and/or Husky voltage at the start of the discharge.  
2. Integrate the Solix output wattage over time (or use a known constant load).  
3. Remaining capacity = starting capacity estimate − energy delivered.  
4. Periodically cross-check against voltage and Solix %.

**Recommended workflow during discharge**:
- Monitor Solix % as the primary real-time SOC indicator.
- Watch Husky pack voltage and the lowest cell voltage as a safety/backup indicator.
- If the Solix % and the voltage-based estimate diverge significantly, trust the voltage for protection limits and investigate equalization or metering drift.
- Near the bottom of the cycle, voltage becomes a much better indicator; stop before either BMS cuts off if you want to preserve cycle life.

---

## 6. Operational Notes and Cautions

- **Connect at similar SOC**. Large differences produce high equalization currents.
- **Cable and fuse sizing**. The interconnect must handle the expected current (potentially tens of amps continuously, higher peaks). Use appropriately rated cabling, connectors, and fusing/breakers on the bus.
- **Polarity and connector integrity**. Confirm polarity carefully. Secure all connections.
- **BMS independence**. Each pack’s BMS protects its own cells. The Solix will not “see” individual Husky cell voltages.
- **Temperature**. Both systems have defined operating ranges; extreme temperatures reduce available capacity and can trigger protective limits.
- **Warranty / official support**. This is a community-documented modification of the expansion port. It is not an official Anker configuration.
- **Monitoring advantage**. The Husky Bluetooth data gives you the high-resolution voltage visibility that the Solix alone lacks, making the combined system easier to understand and safer to operate.

---

## Summary

The paralleled Solix F3800 + Husky V2 system behaves as a single ~9 kWh 16S LiFePO4 battery bank with a shared voltage bus. Pack voltage follows the classic flat LFP curve (roughly 48–57 V operating, ~54.4 V fully charged rested). Total nominal capacity is 8,960 Wh. The Solix percentage provides a convenient real-time estimate of remaining capacity, while the Husky’s Bluetooth cell and pack voltage data supplies precise voltage confirmation and individual cell health visibility.

This combination gives both expanded energy storage and improved observability compared with the Solix alone.

*Document based on published manufacturer specifications and community testing of the Solix expansion-port parallel technique (NoiseLab and others). Always verify connections and monitor the system carefully in your own installation.*
