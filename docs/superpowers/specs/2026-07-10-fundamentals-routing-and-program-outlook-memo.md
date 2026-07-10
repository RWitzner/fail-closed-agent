# Beslutningsmemo: fundamentals-rutevalg + ærlig programvurdering (2026-07-10 aften)

- **Status:** MEMO — dokumenterer diskussionen mellem Robin og Claude efter GPT-reviewet af fundamentals-
  packet rev 3 (`cc252ff`). **Ingen beslutning er truffet; intet i dette memo autoriserer noget.** Åbne
  beslutninger er listet nederst. Packet'en (`2026-07-10-fundamentals-longterm-research-packet.md`, rev 3)
  og GPT-handoff'en er de autoritative artefakter; dette memo tilføjer Claudes anbefaling + den strategiske
  samtale, så en frisk session (og Robin) ikke skal rekonstruere den.
- **Sprog:** dansk (Robins beslutningsmateriale). Facts / vurderinger er markeret.

## 1. Rutevalget efter GPT-verdiktet — Claudes anbefaling: "forward-først" (modificeret rute a)

GPT's verdikt (RECONSIDER-EXPERIMENT) stillede tre ruter: (a) F−1 procurement-review først, (b) direkte til
paid-data-beslutningen, (c) parkér linjen. **Claudes anbefaling er en modificeret (a): gør FORWARD
shadow-testen til hovedsporet, degradér den historiske screen til et valgfrit/betinget værktøj, og tag F−1
med som billig sideleverance.** Ikke (b), ikke (c).

**Fakta bag anbefalingen (alle etableret af de to review-pas):**
1. Den eneste promotion-bærende evidens linjen kan producere er FORWARD (papir-tid efter prædeklarationen).
2. Forward-uret løber i år (~2 år for ~20 månedlige observationer) og starter først når forward-sporet
   starter — hver måneds forsinkelse er evidens tabt for altid.
3. Survivorship-, security-master- og LLM-hindsight-problemerne er HISTORISK-rekonstruktions-problemer; de
   findes ikke forward (EDGAR er PIT by construction fra i dag; delistings observeres når de sker; ingen
   kender fremtiden). De ~80% af F0-omkostningen er netop den del, forward-sporet ikke behøver.

**Dominans-argumentet (vurdering):** 2-års-uret findes i ALLE grene; valget er ikke "vent 2 år eller ej"
men "start det eneste ur der tæller nu, billigt" versus "brug måneder/penge først og start så alligevel
uret". Forward-først dominerer, medmindre man ville satse penge på en historisk backtest alene — hvilket
disciplinen (og GPT) netop forbyder.

**Hvad forward shadow-testen er:** den frosne regel (universe-regel, ROA/E-yield/issuance, top-20) beregnes
hver måneds-ultimo på LIVE PIT-data og journaliseres UDEN ordrer (samme S1/S9-urørte posture som
observe-sessionen), scoret mod SPY TR + equal-weight forward. Første beslutningsdato ville være
2026-07-31. Evalueringskriterier prædeklareres FØR første beslutning (garden-of-forking-paths gælder også
evaluering). Kræver: rev 4/forward-packet (drafting+review — nyt mandat-ask), derefter separat build-go.
Strategisk sidegevinst: bygger præcis den infrastruktur F2 (LLM-research-overlayet) alligevel kræver — en
mekanisk forward-baseline at slå. Den historiske screen kan stadig laves senere (evt. på købt data) som
rejection/kalibrering; den blokerer ikke længere.

**Hvorfor ikke (b):** paid data nu køber ærlighed til en screen, der stadig kun kan afvise — før vi ved om
afvisningsværktøjet er noget værd for Robin. Bliver relevant senere (kalibrering/sizing, family-2-design).
**Hvorfor ikke (c):** intraday-linjen står formentlig ved sit stop efter M7d (forventet NULL); parkering af
fundamentals-linjen efterlader programmet uden nogen vej til valideret edge. Forward-lanen er næsten gratis
at holde kørende og opsigelig månedligt — parkering sparer lidt og koster optionen.

## 2. Hvorfor simulation ikke kan løse hindsight-problemet (Robins spørgsmål, besvaret)

Robin foreslog: giv en agent et workspace hvor gamle data serveres løbende som var de nye — uden facit.
Svar: **det er præcis hvad backtest-harnesset allerede gør** (PIT-regler, usable-from, rå closes).
Maskineriet er ikke problemet. Lækagen sidder to steder, intet workspace kan hegne inde:
1. **I hvem der valgte reglerne:** strategien blev designet i 2026 af forfattere, der har set 2023-2026.
   En perfekt blindet afspilning af en kontamineret hypotese er stadig en kontamineret test (researcher
   degrees of freedom — valget skete FØR simulationen).
2. **I modellens vægte:** en LLM's viden om udfaldene ligger ikke i en database man kan afskære, men
   distribueret i parametrene. "Lad som om du ikke ved det" virker ikke, og lækagen kan ikke MÅLES — en
   umålelig lækage i optimistisk retning er diskvalificerende for validering.
Varianten "brug en gammel model med cutoff før vinduet" er den ærlige version af idéen, men svag i praksis:
gamle modeller er markant svagere end den man reelt ville bruge (testen måler det forkerte system), de
pensioneres, cutoffs er utætte — og problem 1 består på meta-niveau. Simulationens blivende rolle:
**afvisning** (en strategi der taber selv med bagklogskabens fordele er ægte død) — deraf screen'ens
rejection-only-autoritet i rev 3.

## 3. Ærlig forventningsafstemning (Robins "tabt drøm?"-spørgsmål)

**Claudes subjektive odds (VURDERING, ikke fakta):**
- ~60-70%: programmet ender i den ærlige konklusion "ingen målbar edge — køb indekset". (Bemærk: en
  konklusion de færreste når ærligt; den forhindrer tab, hvilket hidtil har været programmets største
  finansielle bidrag — 0 kr. tabt i markedet, to familier afvist på egne målinger frem for handlet i blinde.)
- ~20-30%: en lille ægte edge på 1-3%/år over S&P 500, valideret over år.
- <10%: noget markant bedre. Lavt fordi kendte faktorer er offentlige og trængte.

**De to reelle afgørere:** (1) **Kapitalskala** — 2%/år på 100k er 2k/år; selv succes betaler aldrig tiden
tilbage ved lille kapital; giver kun finansiel mening ved reel skala eller over årtiers rente-på-rente.
(2) **Upside-placeringen** — ikke faktor-strategien (baseline), men F2/AI-research-laget: bredde-læsning af
filings/regnskaber i skala intet menneske matcher, muligvis i små/kedelige lommer hvor institutioner ikke
gider lede ved små beløb. Uafprøvet, men programmets eneste STRUKTURELLE fordel (modsat hastigheds-
handicappet intradag, som egne målinger lukkede).

**Postur-anbefaling (vurdering):** hold programmet som et billigt, automatiseret baggrundsprogram ($0/md.,
timer ikke dage), lad forward-uret løbe, døm på evidens ved forud-forpligtede datoer. Skalér hverken tid
eller penge før der ER forward-evidens. Planlæg ikke levebrød på det. Realistisk bedste udfald: "et par
procent over indekset drevet af disciplin + AI-bredde" — ikke rigdom; mest sandsynlige udfald: et ærligt
"nej" fundet billigt.

## 4. Open source-idéen (Robins spørgsmål: kan andre få noget ud af repoet?)

**Ja — men ikke dataene:** markedsdata (Databento/Alpaca-IEX) må ikke videredistribueres (licens) og ligger
allerede gitignoreret; strategierne er nullede (ingen alfa at lække/miste). **Det værdifulde er:**
(1) sikkerhedsskelettet (fail-closed gates, to-nøgle-arming, preflight-tokens, hash-kædede journaler,
broker-som-sandhed, kill-switches; 2000 tests, stdlib-only) — relevant langt ud over trading i den
nuværende "AI-agenter med pengeadgang"-bølge; (2) metoden (prædeklaration, stop-regler/søgebudgetter,
adversarielle multi-lens + GPT-cross-reviews) — "pre-registreret forskning" anvendt på trading findes
nærmest ikke offentligt; (3) de ærlige nuller med målt omkostningsdekomposition (file-drawer-problemet).
Bedste hook: *"En autonom handelsagent, der endnu ikke har handlet én eneste gang — og det er pointen."*
**Forudsætninger før publicering:** scrub-runde (bl.a. konto-last4 "REDACTED" står i CLAUDE.md; git-historik
auditeres), licensvalg (MIT/Apache + ikke-rådgivning-disclaimer), outsider-README (CLAUDE.md/PLAN.md er
interne). ~1-2 dages arbejde. **Status: idé — ingen beslutning.**

## 5. Åbne beslutninger (alle Robins)

1. **Fundamentals-rutevalget:** (a) F−1-go / **(a′) forward-først (Claudes anbefaling — kræver mandat til
   rev 4/forward-packet-drafting + review, derefter separat build-go)** / (b) paid-data-beslutning direkte /
   (c) parkér.
2. **Track D-drillen** (passiv statuses-lytter, lille build) — go/nej.
3. **Open source-pakken** — go/nej (ved go: audit- og pakkeplan først).
4. (Allerede autoriseret, venter kun på kalenderen: M7d-kørslen ~15/7. Robins stående to-do: regenerér
   Alpaca-nøglerne.)
