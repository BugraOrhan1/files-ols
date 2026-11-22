# Phase 35 - acceptatie

**SOFTWARE_READY_WITH_LIMITATIONS.** De geisoleerde softwarecontroles slagen,
maar zeven worker-tests zijn bewust niet uitgevoerd. Dit is geen
onvoorwaardelijke productievrijgave.

**PARSER_PARTIALLY_VALIDATED; MATCHING_PARTIALLY_VALIDATED.** De 31
aswaarnemingen zijn reproduceerbaar maar niet onafhankelijk gevalideerd. De
instelbare 95%-grens betreft alleen expliciete bytevergelijking van bestanden
met gelijke, niet-nul lengte; het is geen corpusbrede kandidaatzoekfunctie of
compatibiliteitsbewijs.

**BLOCKED_EXTERNAL_REFERENCE; ECU_COMPATIBILITY_NOT_VALIDATED.** WinOLS
5.96.00 is gevonden, maar niet bediend. Er is geen onafhankelijke export en
er zijn geen WinOLS-velden vergeleken.

**INDEX_STATUS_UNKNOWN; backup NOT_VERIFIED.** De productie-index, queue,
leases en workers zijn niet aangeraakt. De index kan niet eerlijk als voltooid,
actief of geblokkeerd worden geclassificeerd.

De geisoleerde eindrun ontdekte 116 tests: 109 geslaagd, 0 mislukt en 7
overgeslagen. Alle 14 originele fixture-hashes zijn onveranderd.
`PHASE35_STATUS.md` bevat bewijs, beperkingen en reproduceerbare instructies.
