# Definitieve lokale acceptatie

**SOFTWARE_READY_WITH_LIMITATIONS.** De volledige geïsoleerde suite slaagde:
117 geslaagd, 0 mislukt, 0 overgeslagen. Dit is geen onvoorwaardelijke
productievrijgave: onafhankelijke parser- en compatibiliteitsvalidatie ontbreekt.

| Onderdeel | Onafhankelijke status |
|---|---|
| Corpusindex | `INDEX_STATUS_UNKNOWN` |
| Parser | `PARSER_PARTIALLY_VALIDATED` |
| Matching | `MATCHING_PARTIALLY_VALIDATED` |
| WinOLS | `BLOCKED_EXTERNAL_REFERENCE` |
| ECU-compatibiliteit | `ECU_COMPATIBILITY_NOT_VALIDATED` |
| Publicatie | `LOCAL_ONLY_NOT_PUBLISHED` |
| Back-up | `NOT VERIFIED` |

De zeven eerder overgeslagen worker-tests zijn na controle van hun paden
uitgevoerd met tijdelijke databases en bronmappen; concurrency, leases,
pauze/hervatting en herstel slaagden. Een afzonderlijke end-to-end-run
configureerde tijdelijke bronnen en controleerde begrensd hashen, import,
zoeken, dashboardresultaten, bytevergelijking en database-integriteit.
De 14 beschermde originele bestanden zijn aantoonbaar ongewijzigd.

Bij gedeeltelijke metadata worden overeenkomsten, afwijkingen, ontbrekende
waarden en parserwaarschuwingen getoond; het metadata-oordeel blijft bij
conflicterende identifiers onbeslist. Een exacte bronhash bevestigt alleen
identieke bestandsinhoud. Een instelbare 95%-drempel vergelijkt uitsluitend expliciet gekozen,
even grote niet-lege bestanden byte voor byte op dezelfde offset. Dit is geen
corpusbrede zoekfunctie, kans of bewijs van ECU-compatibiliteit.

WinOLS 5.96 is geïnstalleerd maar niet bediend. De 31 assen zijn geregistreerd,
maar hun bronwaarden zijn niet onafhankelijk vastgesteld. De pakketexportmappen
zijn leeg en de bestaande gebruikers-BIN's hebben geen onafhankelijke
versie-/projectkoppeling. Nodig is een herleidbare WinOLS-export van project- en
software-ID, maprelaties, dimensies en beschikbare asnamen, identifiers,
eenheden, adressen en datatypes, met bronhash, WinOLS-versie en exportinstellingen.

De productie-index, queue, lease en worker zijn niet geopend of gewijzigd; de
indexstatus blijft onbekend. Details en bewijs staan in `PHASE35_STATUS.md` en
`reports\final_acceptance_unittest.log`, `reports\final_acceptance_workers.log`,
`reports\final_acceptance_e2e.json` en
`reports\final_acceptance_fixture_hashes.json`.

## Dashboard: BIN-selectie en export

De bestaande ECU File Workbench bevat nu een keuzemenu en exportknop voor
geregistreerde binaire referenties en handmatig opgezochte, reeds
geïndexeerde BIN/ORI-locaties. Versienamen zijn onbevestigde labels;
OLS-slots zonder bewezen extractie worden niet geëxporteerd. De gekozen bron
wordt opnieuw gehasht, ongewijzigd gekopieerd en met een JSON-manifest
gecontroleerd. De geselecteerde bytevergelijking bewijst geen compatibiliteit.

De geïsoleerde volledige suite slaagde met 128 tests, 0 mislukt en 0
overgeslagen. In een afzonderlijke browser met synthetische testdata is de
tweede van twee kandidaten geselecteerd en via de dashboardknop geëxporteerd;
bron en uitvoer hadden SHA-256
`cd2f12f0a91a35dd74babe64932ae7a82bde944e36dc140d3f99b6ddc720e4d3`.
De 14 beschermde fixture-hashes bleven ongewijzigd. Deze resultaten valideren
de softwareworkflow, niet de compatibiliteit van echte ECU-bestanden.
Zie de README voor starten en bediening.
