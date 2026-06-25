# VYOM — Test Query List

## Complete VYOM Query Bank

---

### Category 1 — Conversational (no tool calls, instant response)

These route to the `respond` node directly — no DB, no LLM tool use, sub-2s.

```
Hi
```
```
Hello
```
```
What can you do?
```
```
What is VYOM?
```
```
How does the agent work?
```
```
What satellite data do you use?
```
```
What is ResourceSat?
```
```
What is NDWI?
```
```
What is NDVI?
```
```
What is NBR and what is it used for?
```
```
What is the difference between LISS3 and LISS4?
```
```
Why does LISS4 not support burn severity analysis?
```
```
What is DOS calibration?
```
```
What is a Cloud-Optimized GeoTIFF?
```
```
What is the NDWI flood threshold you use?
```
```
What hazards can you analyse?
```
```
Which events are registered in the system?
```
```
What does cloud cover mean for flood analysis?
```
```
Why is optical satellite data limited during monsoons?
```
```
What is a pre-event window?
```
```
What is the difference between event and post-event window?
```
```
What is surface reflectance?
```
```
What indices are available for flood mapping?
```
```
Can you forecast floods?
```
```
Can you analyse Sentinel data?
```
```
What is MNDWI?
```
```
What is a spectral index?
```
```
Thank you
```
```
What events do you have data for?
```

---

### Category 2 — Discovery (light tool calls: list_events, check_coverage, list_scenes)

```
List all available events
```
```
What events have data ingested?
```
```
How many scenes are available for Kerala 2018?
```
```
How many scenes are available for each ingested event?
```
```
What temporal windows are available for Bihar 2019?
```
```
Does VYOM have data for the Assam 2022 flood?
```
```
Do you have data for the Uttarakhand wildfire?
```
```
What is the AOI for the Marathwada drought event?
```
```
Do you have any data over the Arabian Sea?
```
```
Do you have data for Chennai floods?
```
```
Is there data available for Odisha cyclone?
```
```
What sensors were used for the Sikkim 2023 landslide?
```
```
Which events support burn severity analysis?
```
```
Which events have pre-event baseline scenes?
```
```
What is the date range of scenes for Assam 2022?
```
```
How many event-window scenes exist for Bihar 2019?
```
```
What is the average cloud cover for Kerala 2018 scenes?
```
```
List scenes for wildfire_uttarakhand_2016 in the pre-event window
```
```
Which Kerala 2018 scenes have cloud cover below 5%?
```
```
List the post-event scenes for Assam 2022
```

---

### Category 3 — Kerala 2018 Flood (most complete dataset)

```
How did water area change across all temporal windows for the Kerala 2018 floods?
```
```
What is the flood extent for the event-window scene in Kerala 2018?
```
```
Show NDWI statistics for the best post-event scene in kerala_periyar_2018
```
```
Which Kerala 2018 scene has the lowest cloud cover in the post-event window?
```
```
Compare NDWI across pre-event, event, and post-event windows for Kerala 2018
```
```
What was the water area percentage during the Kerala 2018 flood event?
```
```
Export a false colour image for the clearest Kerala 2018 post-event scene
```
```
Show me the flood map for Kerala 2018
```
```
What is the NDVI trend across annual scenes for Kerala 2018?
```
```
How many annual baseline scenes exist for Kerala 2018 and what is their average NDWI?
```
```
Which Kerala 2018 scene covers the Periyar river basin best?
```
```
Show the NDWI change between the best pre-event and best post-event scene for Kerala 2018
```
```
What percentage of the Kerala AOI was flooded during the 2018 event?
```
```
Export the NDWI index map for the Kerala 2018 event-window scene
```
```
Find the scene with highest water_area_pct in the Kerala 2018 post-event window
```

---

### Category 4 — Assam 2022 Flood (event + post only, LISS4)

```
How did water area change between event and post-event windows in Assam 2022?
```
```
What is the peak flood extent in km² for the Assam Brahmaputra 2022 flood?
```
```
List all event-window scenes for Assam 2022 sorted by cloud cover
```
```
Which Assam 2022 scene shows the maximum water area percentage?
```
```
Show NDWI statistics for the Assam 2022 flood event window
```
```
Did the flood water recede in the Assam 2022 post-event window?
```
```
Export a flood map for the worst Assam 2022 event-window scene
```
```
What was the average water_area_pct across all Assam 2022 event scenes?
```
```
Show me the false colour image for the clearest Assam 2022 event scene
```
```
How many km² of the Brahmaputra basin were inundated during the 2022 event?
```
```
Compare water area between the 12 event-window and 30 post-event scenes for Assam 2022
```
```
Which Assam 2022 post-event scene shows the lowest residual flooding?
```

---

### Category 5 — Bihar 2019 Flood (richest event window — 33 scenes)

```
How did flooding evolve across pre-event, event, and post-event windows in Bihar 2019?
```
```
Which Bihar 2019 scene has the highest water area percentage?
```
```
Show the flood extent for the worst flooding scene in Bihar 2019
```
```
Did water recede in the Bihar 2019 post-event window compared to the event window?
```
```
Find the best pre-event and event-window scene pair for Bihar 2019 and compute the NDWI change
```
```
What is the average water_area_pct across all 33 event-window scenes for Bihar 2019?
```
```
List the top 5 Bihar 2019 event scenes ranked by water area percentage
```
```
Export a false colour image for the peak flooding scene in Bihar Kosi Ganga 2019
```
```
Show NDWI and NDVI statistics for the Bihar 2019 event window
```
```
Which Bihar 2019 scenes use LISS3 and which use LISS4?
```
```
What is the flood area in km² for the peak event scene in Bihar 2019?
```
```
Compare pre-event baseline water area to peak event water area for Bihar 2019
```
```
Show the NDWI change map between the Bihar 2019 pre-event and post-event scenes
```

---

### Category 6 — Wildfire Uttarakhand 2016 (NBR/MNDWI available, LISS3 only)

```
What is the burn severity for the Uttarakhand 2016 wildfire?
```
```
Show NBR statistics for the event-window scenes in Uttarakhand 2016
```
```
Compare NBR between pre-event and post-event windows for the Uttarakhand wildfire
```
```
Which Uttarakhand 2016 scene shows the most severe burn — lowest NBR?
```
```
What is the burn scar extent in km² for the Uttarakhand 2016 wildfire?
```
```
Export the NBR change map between pre-event and post-event for Uttarakhand 2016
```
```
How many pre-event baseline scenes exist for the Uttarakhand wildfire?
```
```
Show NDVI before and after the Uttarakhand 2016 fire — did vegetation recover?
```
```
List event-window scenes for wildfire_uttarakhand_2016 with their NBR statistics
```
```
Export a false colour image for the most burned scene in Uttarakhand 2016
```
```
What percentage of the Uttarakhand AOI shows burn scarring in the post-event scenes?
```
```
Compare MNDWI before and after the Uttarakhand 2016 fire
```

---

### Category 7 — Drought Marathwada 2016 (NDVI anomaly, pre/event/post)

```
How did vegetation health change across pre-event, event, and post-event windows for the Marathwada drought?
```
```
Compare NDVI between the 2013 baseline and the 2015 drought peak for Marathwada
```
```
Did vegetation recover in Marathwada by the post-event 2016 window?
```
```
What is the NDVI drop between pre-event and event windows for the Marathwada drought?
```
```
Show NDVI statistics for all three windows of drought_marathwada_2016
```
```
Which Marathwada scene shows the lowest NDVI — peak vegetation stress?
```
```
List all event-window scenes for drought_marathwada_2016 with NDVI statistics
```
```
Export a false colour image showing vegetation stress in the Marathwada drought event
```
```
How much did average NDVI recover from the 2015 drought to the 2016 post-event period?
```
```
What was the NDVI anomaly magnitude during the Marathwada 2016 drought?
```

---

### Category 8 — Landslide Sikkim GLOF 2023 (event + post, highest cloud)

```
What vegetation change occurred after the Sikkim GLOF 2023 landslide?
```
```
Which Sikkim 2023 scene has the lowest cloud cover?
```
```
Compare NDVI between event and post-event scenes for the Sikkim 2023 landslide
```
```
Show the NDWI change between the best event and post-event scene for Sikkim GLOF 2023
```
```
Did water area increase after the Sikkim 2023 glacial lake outburst?
```
```
List all scenes for landslide_sikkim_glof_2023 with their cloud cover and NDVI
```
```
Export a false colour image for the clearest Sikkim 2023 post-event scene
```
```
What is the NDVI loss in the Sikkim GLOF 2023 landslide area?
```
```
Show me the water extent after the Sikkim 2023 GLOF event
```

---

### Category 9 — Cross-Event Comparisons (most impressive for demo)

```
Which of the three flood events — Kerala 2018, Assam 2022, or Bihar 2019 — had the highest peak water area percentage?
```
```
Rank all flood events by severity based on water_area_pct
```
```
Compare water_area_pct across Kerala, Assam, and Bihar flood events
```
```
Which event has the most scenes and the best temporal coverage?
```
```
Which flood event had the fastest water recession after the event?
```
```
Compare average cloud cover across all 6 ingested events
```
```
Which event has the cleanest data — lowest average cloud cover?
```
```
How does the Bihar 2019 flood compare to the Kerala 2018 flood in terms of peak inundation?
```
```
Which event has the most complete pre and post event window coverage?
```
```
Compare NDVI recovery rates after the Uttarakhand wildfire versus the Sikkim landslide
```
```
Which event is best suited for change detection analysis and why?
```
```
Across all ingested events, which has the largest flooded area in km²?
```

---

### Category 10 — Out-of-Scope / Refusal Tests (agent should decline gracefully)

These confirm the safety policy works — good to show to your guide:

```
What is the flood situation in Chennai right now?
```
```
Forecast how bad the next Assam flood will be
```
```
Analyse the Mumbai 2005 floods
```
```
Show me Sentinel-2 data for Kerala
```
```
What is the stock price of ISRO?
```
```
Do you have data for Bangladesh floods?
```
```
Can you analyse SAR data?
```
```
Show flood data for Pakistan
```

---

### Quick Reference — Query → Expected Tools

| Query type | Tools called |
|---|---|
| Hi / what can you do | none |
| List events | `list_events` |
| Do you have data for X | `list_events` → `check_coverage` |
| Scene list / cloud filter | `check_coverage` → `list_scenes` |
| Window statistics | `check_coverage` → `compare_windows` |
| Flood extent / km² | `check_coverage` → `list_scenes` → `flood_extent` |
| Change map | `check_coverage` → `find_scene_pairs` → `compute_change` |
| False colour export | `check_coverage` → `list_scenes` → `export_png` |
| Cross-event rank | `list_events` → `check_coverage` × N → `compare_events` |
| Out-of-scope | none — polite refusal |

---

### Ingested Events Summary (as of 2026-06-25)

| Event | Scenes | Date Range | Avg Cloud | Sensors |
|---|---|---|---|---|
| assam_brahmaputra_2022 | 42 | 2022-05-15 → 2022-11-23 | 19.7% | LISS4 only |
| bihar_kosi_ganga_2019 | 64 | 2018-12-27 → 2020-02-27 | 20.2% | LISS3 + LISS4 |
| drought_marathwada_2016 | 47 | 2013-06-21 → 2016-12-28 | 8.7% | LISS3 only |
| kerala_periyar_2018 | 88 | 2015-01-04 → 2018-12-21 | 7.2% | LISS3 + LISS4 |
| landslide_sikkim_glof_2023 | 24 | 2023-10-05 → 2024-03-21 | 25.5% | LISS3 + LISS4 |
| wildfire_uttarakhand_2016 | 73 | 2014-01-28 → 2016-07-13 | 8.6% | LISS3 only |
| **TOTAL** | **338** | | | |

### Window Breakdown per Event

**assam_brahmaputra_2022** (flood — NDWI primary index)
- event: 12 scenes | post_event: 30 scenes
- No pre-event or annual baseline — LISS4 only (no SWIR → no MNDWI/NBR)

**bihar_kosi_ganga_2019** (flood — NDWI)
- pre_event: 1 | event: 33 | post_event: 30
- LISS3 scenes (13) have full SWIR indices; LISS4 (51) are NDVI+NDWI only

**drought_marathwada_2016** (drought — NDVI anomaly)
- pre_event: 17 | event: 17 | post_event: 13
- LISS3 only → full index suite (NDVI, NDWI, MNDWI, NBR)

**kerala_periyar_2018** (flood — NDWI) ← most complete
- annual: 64 | pre_event: 5 | event: 1 | post_event: 18
- LISS3 (70) + LISS4 (18); only 1 optical event-window scene (cloud/coverage gap)

**landslide_sikkim_glof_2023** (landslide — dNDVI)
- event: 11 | post_event: 13
- Highest cloud cover (25.5%); LISS3 (3) + LISS4 (21)

**wildfire_uttarakhand_2016** (wildfire — NBR)
- annual: 54 | pre_event: 13 | event: 3 | post_event: 3
- LISS3 only → full MNDWI/NBR available; good pre-event coverage

### What will and won't work

| Query type | Works? | Reason |
|---|---|---|
| `compare_windows`, `list_scenes`, metrics | ✅ Always | reads cached DB rows |
| `flood_extent`, `export_png`, `compute_change` | ✅ If COGs present on disk | opens `data/ard/*.tif` |
| NBR / MNDWI on Assam | ❌ Expected fail | LISS4 has no SWIR |
| Event-window flood map Kerala | ⚠️ Sparse | only 1 optical event scene |
| Bihar event window analysis | ✅ Best | 33 event scenes available |
| Sikkim analysis | ⚠️ High cloud | 25.5% avg cloud, careful scene selection needed |
