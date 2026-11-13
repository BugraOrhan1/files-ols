# OLS Tuner – leerketen, generator en Windows-app

Status: **TECHNISCHE REPRODUCTIE GESLAAGD; BLINDE GENERATIETEST OP ECHTE DATA NIET UITGEVOERD.**
De uitspraak “de app maakt zelfstandig Stage 1” wordt **niet** gedaan: daarvoor ontbreken
(in deze omgeving) een tweede onafhankelijk project binnen dezelfde ECU-familie en een echt
AutoTuner-origineel met apart gehouden, deskundig beoordeelde referentie.

## Wat er gebouwd is

| Onderdeel | Bestand | Functie |
| --- | --- | --- |
| Versies uit OLS | `ols_platform/tuning/ols_versions.py` | Leest de RevisionTag (versienamen + WinOLS-checksumnotitie per versie) en de versie-images. |
| Identificatie | `ols_platform/tuning/identity.py` | Onderscheidt ruwe uitlezing, OLS-project, ZIP/7z/RAR (ook hernoemd naar .bin), versleuteld (Slave), tekst, leeg; leert ECU/HW/SW-tokens die letterlijk in de bytes staan. |
| Leren | `ols_platform/tuning/learn.py` | Ontleedt paren in bewerkingsgebieden, leert welke gebieden samen veranderen (support), classificeert kaart (16-bit, bytevolgorde bepaald) vs. patch, leert per cel exact/ratio/delta + grenzen + toepasbaarheid. |
| Pakket | `ols_platform/tuning/package.py` | Deterministisch, inhoud-gehasht pakket (`.olstune`), lokale opslag met actief pakket, historie en terugzetten; integriteitscontracten per familie. |
| Bibliotheek bijwerken | `ols_platform/tuning/library.py` | Hervatbare batchverwerking van bronmappen (checkpoint per project, retries, geen verlies bij netwerkuitval), paarbevestiging, opslag van paren, training, uitsluiting testprojecten incl. duplicaten/afstammelingen. |
| Maak getunde BIN | `ols_platform/tuning/generate.py` | Herkent familie/variant, controleert indeling en context per gebied, weigert al getunede/gemengde invoer, past alleen samenhangend geheel toe. |
| Export | `ols_platform/tuning/export.py` | Tijdelijk bestand → onafhankelijke herlezing van schijf → 0 wijzigingen buiten toegestane gebieden → atomisch publiceren zonder overschrijven → rapport. Onbekende integriteit blokkeert. |
| Evaluatie | `ols_platform/tuning/evaluate.py` | Vergelijking met apart gehouden referentie en twee basislijnen (ongewijzigd origineel, donor-diff-kopie). |
| CLI | `python -m ols_platform.tuning …` | `train`, `generate`, `packages`, `rollback`, `install-package`, `export-package`, `contract`. |
| App | `ols_platform/tuning/app.py` | Nederlandstalig hoofdscherm + scherm “Bibliotheek en diagnose”; knop **Bibliotheek bijwerken** gebruikt dezelfde `Library.scan` + `Library.train` als `train`. |
| Build | `packaging/OLSTuner.spec`, `.github/workflows/windows-build.yml` | PyInstaller 6.11.1 onedir-build, ZIP + SHA-256, test van precies dat archief. |

## Gemeten bevindingen op de 8 OLS-voorbeelden

1. **Slotgrens py-ols is 4 bytes verschoven.** Het versie-image is
   `bestand[blob_offset + 4 : blob_offset + 4 + blob_size]`. Dit is SHA-256-identiek aan alle
   14 onafhankelijke WinOLS-exports in de repo (4 projecten). De bestaande reader leest
   “marker + payload” en zit dus 4 bytes ernaast; de tuningketen gebruikt de gecorrigeerde grens.
2. **Versienamen** komen uit de RevisionTag (bijv. `1 (Original,\t Stage 1:\t NOCS)`); volgorde =
   slotvolgorde, bevestigd met dezelfde 14 exports. De slotnamen van py-ols zijn verschoven en worden
   niet gebruikt.
3. **Checksumstatus van de referenties:** BMW, VW, Audi, MB: `NOCS` (WinOLS heeft niet gecorrigeerd);
   Peugeot: `CS+, OLS280 2.50` (gecorrigeerd door de checksummodule) en `CS-` voor EGR OFF.
4. **Paren:** bevestigde zuivere Stage 1-paren: **BMW 7er MDG1_MD1CP002** (100009.ols) en
   **VW Golf SIMOS18.10** (100004.ols). Uitgesloten: MB C220 CRD3 (identificatie staat niet in de
   bytes), Audi A8 “stage 1 = Vmax 280” (gecombineerd), alle P&B/EGR/Stage 2-varianten (alleen
   gebruikt om gemengde invoer te herkennen).
5. **Kaartdefinities:** het OLS-project van BMW bevat 4 (naamloze) kaarten, VW 0. De 4558 resp. 9978
   gewijzigde bytes liggen grotendeels daarbuiten; de leerfunctie leidt de gebieden daarom zelf af
   (BMW: 282 gebieden, 274 als 16-bit kaart herkend, 8 overig; VW: 79 gebieden, 58 kaart, 21 overig).
   Physical units: de aanwezige definities hebben factor 1.0 en geen eenheid → afwijkingen worden in
   ruwe eenheden gerapporteerd.
6. **C-parsers** jinglinsen/ols-parser en JSD-SpeedUP/ols-parser-public verschillen 14 regels
   (alleen een `strdup`-vervanger), zelfde copyrighthouder: **geen onafhankelijk bewijs**.

## Acceptatietests (deze sessie)

| Test | Invoer | Verwachting | Uitkomst | Status |
| --- | --- | --- | --- | --- |
| Bestaande software | `python -m unittest discover -s tests` | alles groen | 173 tests, OK, exit 0; 6 skips zonder `PY_OLS_PATH` (echte-parsertests), 0 skips met py-ols @4c283a8 | GESLAAGD |
| Bekend trainingspaar BMW | export `WinOLS (BMW 7er (Original) …).bin` | byte-identiek aan Stage 1-export | SHA-256 `1e6aa221…` = referentie; 0 wijzigingen buiten gebieden | GESLAAGD |
| Bekend trainingspaar VW | export VW Golf Original | byte-identiek aan Stage 1-export | identiek, 9978 bytes gereproduceerd | GESLAAGD |
| Pakket reproduceerbaar | 2× trainen met dezelfde input | zelfde `package_id`, zelfde uitvoer | gelijk | GESLAAGD |
| Nieuw project toevoegen | synthetisch project erbij | volgende run toont toegevoegd paar | `pairs_added_since_previous` = 1, rollback naar vorige werkt | GESLAAGD (synthetisch) |
| Blinde proef synthetisch | 3 trainingsprojecten, 4e project onbekend | binnen 1 eenheid, 0 buiten gebied, beter dan basislijn | max. afwijking 1 ruwe eenheid; donor-diff 123 | GESLAAGD (alleen mechanisme) |
| **Blinde proef echte data** | – | – | per familie maar 1 project beschikbaar | **NIET UITGEVOERD** |
| Afwijzingen | verkeerde SW zelfde grootte, andere HW, onbekende ECU, al getuned (BMW/VW Stage 1), gedeeltelijk getuned, P&B-invoer, afgekapt, leeg, OLS-project, ZIP, willekeurig/versleuteld, kaart verschoven, cel buiten bereik, onverklaarde patch | specifieke reden, geen BIN | alle gevallen juiste code + Nederlandse reden | GESLAAGD |
| Ongeldige/onbekende checksum | geen contract | export geblokkeerd, geen bestand | `integrity_unknown`, geen bestand | GESLAAGD |
| Onderbrekingen | netwerkfout (retry + `retry_pending` + herstel), batchlimiet + hervatten, stop-signaal, bron onbereikbaar, volle schijf, twee gelijktijdige exports, paden met spaties/Unicode | geen verlies, geen dubbele records, geen halve BIN, origineel ongewijzigd | zoals verwacht | GESLAAGD (gesimuleerd) |
| GUI | lege installatie + volledig pad onder Xvfb (Linux) | eerste-startmelding, BIN, afwijzing | zoals verwacht; schermafbeeldingen gemaakt | GESLAAGD |
| Windows-release | ZIP uit CI uitgepakt op Windows-runner zonder Python op PATH | selftest, GUI-rooktest, echte BMW-reproductie via EXE, afwijzing Audi | zie CI-run van de gepushte commit | zie eindrapport |
| AutoTuner-herkenning / voertuig | – | – | geen AutoTuner, geen voertuig, geen testbank beschikbaar | NIET UITGEVOERD |

## Ondersteunde combinaties (bewijsniveau REPRODUCTION_ONLY)

| Familie | Grootte / leesvorm | HW | SW | Profiel | Projecten |
| --- | --- | --- | --- | --- | --- |
| Bosch MDG1_MD1CP002 (BMW 7er G12 50d, B57) | 2.621.440 B; importnaam “…-obd-vr.bin” (OBD/virtueel, niet geverifieerd) | 8KTB1-000047D1-028 | B57D30S0-G1 / B57S0_RDE / S15A-17-07-504-FS | Stage 1 | 1 |
| Continental SIMOS18.10 (VW Golf 2.0 TSI) | 5.767.168 B; leesvorm onbekend | 5G0906259N / SCG0051000000 | 5G0906259N | Stage 1 | 1 |

Bewezen is alleen: hetzelfde origineel → dezelfde Stage 1 als de WinOLS-referentie. Een ander
voertuig binnen dezelfde softwarevariant wordt alleen verwerkt als alle cellen binnen het geleerde
bereik vallen (tolerantie 15 %) en de kaartindeling exact overeenkomt; dat is **niet** blind getest op
echte data. Zonder vastgelegd integriteitscontract blokkeert de export.

## Integriteit

Er is in deze sessie **geen** ECU-checksum- of handtekeningmethode geverifieerd. De referenties zelf
zijn `NOCS`. De app schrijft pas een gebruiksklare BIN als de gebruiker per familie vastlegt dat zijn
AutoTuner-route de checksum bij schrijven corrigeert (contract `FLASH_TOOL_CORRECTS_ON_WRITE`, met
onderbouwing); het rapport noemt die resterende stap. Dit contract is door de gebruiker te
controleren; het is niet door deze software bewezen.

## GitHub-bronnen

| Bron | Revisie / licentie | Bevinding | Gebruik |
| --- | --- | --- | --- |
| TheFlashBold/py-ols | 4c283a8, GPLv3 | Metadata en slots bruikbaar; slotgrens 4 bytes verschoven; versienamen niet correct; weinig/naamloze kaarten | Alleen bij Bibliotheek bijwerken, door gebruiker aangeleverd; **niet gebundeld** |
| jinglinsen/ols-parser | c77616e, 0BSD | RevisionTag, segmenten zichtbaar | Idee RevisionTag; eigen Python-implementatie |
| JSD-SpeedUP/ols-parser-public | 0640c3b, 0BSD | 14 regels verschil met jinglinsen → zelfde code | Niet als onafhankelijk bewijs geteld |
| NXT-Tronic/winols-mcp | 7bd849d, MIT | Vereist WinOLS + licentie External Control (OLS530); eigen README: niet getest tegen live WinOLS | Niet gebruikt (geen WinOLS in deze omgeving) |
| mohammedbenserya/pywinauto | 22b00fd, geen licentie | Eén GUI-klikscript, vaste paden/slaaptijden | Niet geschikt als betrouwbare exportroute; geen licentie |
| 2CRPerformance/LinOLS | 2bc16a7, GPL | Python-hexviewer | Niet gebundeld |
| reproteq/phpOLS | e8d57be, MIT | PHP CSV-kaartlezer | Niet gebruikt (geen CSV-export beschikbaar) |
| reproteq/winols_damos_translator | ab5cd9d, geen licentie | Vertaalt via googletrans (online) | Niet gebruikt: stuurt namen naar externe dienst |
| infinitehexsearcher/Ghidra-WinOLS-CSV-Importer | e6dada5, GPL | Ghidra-plugin | Niet gebruikt |
| liushengjia-AAL/shengjia | cdcbe28, geen licentie | Achtergrond (o.a. md1-platform, checksum-crc-engine) | Alleen achtergrond; niets overgenomen |

## Wat nodig is voor de beslissende proef

1. Minimaal **twee onafhankelijke projecten (verschillend origineel) per familie** met zuivere
   Stage 1-versie, uit `\\Dell\d\Database 1-4` en `\\Dell\d\Default`; draai daar
   `python -m ols_platform.tuning train --py-ols <map> --source "Database 1=\\Dell\d\Database 1" …
   --holdout <testproject>`.
2. Een **echt AutoTuner-origineel** (onversleutelde lezing) van een voertuig waarvan een deskundig
   beoordeelde Stage 1 apart wordt gehouden.
3. Bevestiging per familie van de **checksumroute** (AutoTuner-correctie bij schrijven of WinOLS
   checksummodule), liefst met een WinOLS-checksumcontrole van de gegenereerde BIN.
