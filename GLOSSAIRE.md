# Glossaire

Termes techniques et métier utilisés dans ce dépôt. Les définitions sont celles qui s'appliquent
**dans ce contexte** — recharge de véhicules électriques et ingestion de données temps réel.
La colonne EN donne le terme anglais tel qu'il apparaît dans le code et les autres documents.

## 1. Métier — recharge de véhicules électriques

| Terme FR | EN | Définition |
|---|---|---|
| Véhicule électrique | EV | Véhicule rechargeable sur le réseau électrique. |
| Opérateur de points de charge | CPO — *Charge Point Operator* | Exploite physiquement les bornes : installation, maintenance, disponibilité, publication des statuts. **Motor Fuel Group est le CPO de ce flux.** |
| Opérateur de mobilité | eMSP — *e-Mobility Service Provider* | Vend l'accès à la recharge à l'automobiliste (badge, application, facturation). Souvent distinct du CPO. |
| Site / station | *Location* | Emplacement géographique regroupant plusieurs bornes. 583 dans ce flux. |
| Borne / point de charge | *Charge point* | Terme commercial ambigu : peut désigner le mobilier physique ou un point de service. En analyse, préférer EVSE. |
| EVSE | *Electric Vehicle Supply Equipment* | **Unité qui alimente un véhicule à la fois.** C'est l'entité de comptage retenue ici : 2 944 EVSE dans ce flux. Un EVSE peut exposer plusieurs connecteurs, mais un seul est actif à la fois. |
| Connecteur | *Connector* | Prise physique (CCS, CHAdeMO, Type 2). Plusieurs connecteurs par EVSE — **jamais compté comme session indépendante**, sinon on double-compte. |
| Recharge AC / DC | *AC / DC charging* | AC = courant alternatif, lent (~7–22 kW). DC = continu, rapide à ultra-rapide (50–350 kW). Détermine l'ordre de grandeur des durées attendues. |
| Puissance / énergie | *Power (kW) / energy (kWh)* | La puissance est un débit instantané, l'énergie un cumul. **Ce flux ne contient ni l'un ni l'autre.** |
| Session de recharge | *Charging session* | Période pendant laquelle un véhicule est effectivement alimenté. Aucune définition officielle dans ce flux : voir la reconstruction dans `RESULTS.md`. |
| Session de facturation | *Billing session* | Session commerciale, du branchement au paiement. **Différente** de la session technique : inclut souvent le temps de stationnement après fin de charge. |
| CDR | *Charge Detail Record* | Enregistrement officiel de fin de session (énergie, durée, tarif, identifiant client). La référence de vérité pour facturer. **Absent ici.** |
| Temps de stationnement | *Dwell time* | Durée totale d'immobilisation de la place, charge terminée comprise. Ce qui compte pour dimensionner un site. |
| Taux d'occupation | *Occupancy / utilisation rate* | Part des EVSE en `CHARGING` à un instant donné. **21,1 % en moyenne dans ce run.** Métrique la plus robuste ici car insensible à la censure. |
| Taux de disponibilité | *Uptime / availability* | Part des EVSE en état de fonctionner. 12,8 % étaient `INOPERATIVE` en fin de run — un problème de fiabilité de parc, distinct de l'usage. |
| Rendement d'actif | *Yield* | Revenu rapporté au capital investi sur un site. Dépend de l'énergie vendue, pas de la durée d'occupation seule. |
| Itinérance | *Roaming* | Accord permettant à un client d'un eMSP d'utiliser les bornes d'un autre CPO. Explique la multiplication des flux hétérogènes. |
| Données ouvertes | *Open data* | Données publiées librement. Ici, obligation réglementaire britannique (*UK Public Charge Point Regulations 2023*) imposant aux CPO de publier la disponibilité en temps réel. |

## 2. Standards et protocoles

| Terme | Définition |
|---|---|
| **OCPI** (*Open Charge Point Interface*) | Protocole d'échange **entre acteurs** (CPO ↔ eMSP ↔ agrégateurs). Définit le module `Locations` utilisé ici, dont le vocabulaire de statuts. Version de référence : 2.2.1. |
| **OCPP** (*Open Charge Point Protocol*) | À ne pas confondre : protocole **entre la borne et son back-office**. Nous ne le voyons jamais depuis un flux ouvert. |
| **Style OCPI** | Le flux MFG suit la *forme* d'OCPI sans garantie de conformité complète. Le code valide donc ce qu'il reçoit au lieu de le supposer conforme. |
| `last_updated` | Horodatage fourni par la source pour un EVSE. **Peut bouger pour un changement de métadonnée de connecteur** — ce n'est donc pas une preuve de changement de statut. |
| **Snapshot complet** | Réponse contenant l'état de *toutes* les entités. C'est le cas ici : aucune API de delta, détecter les changements est le travail du pipeline. |
| **Delta / changements** | Réponse ne contenant que ce qui a changé. Moins coûteux, mais impose de faire confiance à la source pour ne rien omettre. |

### Statuts OCPI rencontrés

| Statut | Sens | Traitement ici |
|---|---|---|
| `AVAILABLE` | Libre et opérationnel | Fin de session valide (454 fins observées) |
| `CHARGING` | En cours d'alimentation | Ouvre une session |
| `INOPERATIVE` | Hors service temporairement | Fin de session valide (62 fins) — fin *anormale*, pas un départ client |
| `OUTOFORDER` | En panne | Fin de session valide (3 fins) |
| `BLOCKED` | Accès physiquement bloqué | Fin valide (non observé sur ce run) |
| `RESERVED` | Réservé | Fin valide (non observé) |
| `PLANNED` / `REMOVED` | Prévu / retiré du parc | Fin valide (aucune fin observée ; 4 EVSE stationnaires en `REMOVED`) |
| `UNKNOWN` | État inconnu de l'opérateur | **Ambigu : la session est écartée**, jamais close arbitrairement (40 EVSE en fin de run) |

## 3. Ingénierie de données

| Terme FR | EN | Définition |
|---|---|---|
| Interrogation périodique | *Polling* | Le consommateur appelle la source à intervalle régulier. Mode utilisé ici (120 s). |
| Notification poussée | *Push / webhook* | La source appelle le consommateur au changement. Autre mode d'ingestion à prévoir pour 100+ sources. |
| Dépôt de fichier | *File drop* | Livraison par fichiers (SFTP, S3). Troisième mode courant. |
| Adaptateur | *Adapter* | Code spécifique à une source, qui traduit son format en observation canonique. **Seule couche à réécrire par source.** |
| Observation canonique | *Canonical observation* | Tuple unique en aval : `(source, location_id, evse_uid, evse_id, status, source_last_updated)`. Tout le cœur générique ne connaît que ça. |
| Réducteur | *Reducer* | Compare le snapshot complet à l'état courant et produit les événements : `INITIAL`, `CHANGE`, inchangé, absent. |
| Point de référence initial | *Baseline* | Première observation d'un EVSE. **N'est jamais un changement** : on ne sait pas ce qui la précédait. |
| Limitation de débit | *Rate limiting* | Restriction imposée par la source. Se manifeste par un **HTTP 429**. 4 occurrences dans ce run. |
| `Retry-After` | — | En-tête indiquant le délai suggéré avant nouvelle tentative. Ici **conservé mais jamais utilisé pour raccourcir** le plancher de 120 s. |
| Repli exponentiel | *Exponential backoff* | Doublement du délai à chaque échec consécutif, plafonné (15 min ici). |
| Idempotence | *Idempotency* | Rejouer la même opération donne le même résultat. Permet de relancer sans corrompre l'état. |
| Transaction atomique | *All-or-nothing transaction* | Un snapshot est appliqué entièrement ou pas du tout. Aucun état partiel n'est observable. |
| Événements immuables | *Immutable events* | Les transitions sont écrites une fois et jamais modifiées. **Conséquence pratique : changer la définition d'une session ne nécessite pas de recollecter.** |
| Registre des tentatives | *Attempt ledger* | Table `poll_runs` : chaque tentative, réussie ou non, avec issue, compteurs, empreinte, durées, erreur. Base de l'audit et de la reprise après redémarrage. |
| Stockage adressé par contenu | *Content-addressed storage* | Le fichier est nommé par l'empreinte **SHA-256** de son contenu : deux snapshots identiques n'occupent qu'un fichier. |
| Déduplication | *Deduplication* | Conséquence du point précédent : 54 réponses stockées en 9 Mo compressés au lieu de 203 Mo bruts. |
| Rejeu | *Replay* | Reconstruire les résultats à partir des données brutes archivées, sans réinterroger la source. |
| Horloge monotone | *Monotonic clock* | Horloge qui ne recule jamais (insensible aux ajustements NTP). Utilisée pour cadencer les requêtes ; l'heure murale UTC ne sert qu'à horodater. |
| Verrou | *Lock* | Verrou de fichier système empêchant deux collecteurs simultanés (sur la base **et** sur l'endpoint). |
| Bail | *Lease* | Verrou à durée limitée, porté par la base. Équivalent multi-machines du verrou de fichier, nécessaire pour 100+ sources. |
| Registre de sources | *Source registry* | Configuration par source : adaptateur, cadence, timeout, limites. Évite de coder en dur 100 pipelines. |
| Contrat de schéma | *Schema contract* | Attente formelle sur la forme d'un payload. Une violation met le payload en quarantaine au lieu de polluer l'état. |
| Quarantaine | *Quarantine* | Zone où atterrissent les payloads invalides, pour inspection sans blocage du pipeline. |
| Fraîcheur | *Freshness* | Délai depuis la dernière donnée valide d'une source. Indicateur d'alerte principal en production multi-sources. |
| SLO | *Service Level Objective* | Objectif chiffré de qualité de service (ex. « 95 % des sources rafraîchies en moins de 10 min »). |
| Partitionnement | *Partitioning* | Découpage physique d'une table (ici : par source et par date d'événement) pour contenir le coût des requêtes. |
| WAL | *Write-Ahead Log* | Mode journal de SQLite : les écritures sont journalisées avant application, ce qui protège d'une coupure en cours d'écriture. |

## 4. Statistique et lecture des résultats

| Terme FR | EN | Définition |
|---|---|---|
| Censure à gauche | *Left censoring* | L'événement avait **commencé avant** le début de l'observation : sa durée est inconnue. **668 cas ici**, exclus de la moyenne. |
| Censure à droite | *Right censoring* | L'événement n'était **pas terminé** à la fin de l'observation. **300 cas ici**, exclus. |
| Dénominateur | *Denominator* | Le nombre d'observations effectivement retenues. **519 sessions complètes sur 1 487 épisodes touchés (35 %)** — à annoncer systématiquement avec une moyenne. |
| Biais de sélection | *Selection bias* | Distorsion due au fait que les cas retenus ne sont pas représentatifs. Ici : les sessions longues ont plus de chances d'être censurées, donc la moyenne est **sous-estimée**. |
| Échantillon de convenance | *Convenience sample* | Échantillon pris parce qu'il était disponible, pas parce qu'il est représentatif. Un opérateur, un lundi soir, deux heures. |
| Quantification / résolution | *Quantisation* | Une cadence de 120 s ne peut ni voir une session plus courte que l'intervalle, ni dater une transition à mieux que ±2 min. |
| Médiane | *Median* | Valeur centrale (23,98 min). Moins sensible aux valeurs extrêmes que la moyenne (26,54 min). |
| P90 — rang le plus proche | *P90, nearest rank* | Valeur en dessous de laquelle se situent 90 % des observations (51,43 min), calculée sans interpolation. |
| p50 / p95 (latence) | — | Percentiles appliqués aux temps de traitement. Le p95 mesure le cas défavorable réaliste, pas le pire absolu. |
| Analyse de sensibilité | *Sensitivity analysis* | Recalcul du résultat sous une hypothèse alternative, pour mesurer sa fragilité. Ici : moyenne « sans trou » (11,82 min) et moyenne tout-temps-d'observation (26,51 min). |
| Artefact | *Artefact* | Effet produit par la méthode de mesure, pas par le phénomène. La moyenne « sans trou » en est un : le filtre tronque mécaniquement les sessions longues. |
| Microbenchmark | — | Mesure de performance isolée d'un composant. Indique une marge locale, **ne prédit pas** le comportement réseau multi-sources. |
