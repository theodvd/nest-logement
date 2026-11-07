---
name: entretien-recherche
description: Installe nest et mène l'entretien de recherche de logement de l'utilisateur (sa vie, ses habitudes, ses arbitrages) pour écrire profil.yaml, ranger ses clés dans .env, installer la tâche planifiée et lancer un premier passage à blanc. Utiliser au premier lancement (pas de profil.yaml), quand l'utilisateur veut installer nest, définir ou revoir ce qu'il cherche, dit « mes critères ont changé », « pose-moi des questions sur ma recherche », ou trouve que les scores des notifs ne collent pas à ses envies.
---

# Installation et entretien de recherche

nest surveille Leboncoin (et, en option, Gens de Confiance), calcule le vrai trajet jusqu'au
lieu de stage, fait juger chaque annonce par Claude d'après un profil, et envoie une notif
sur le téléphone avec un brouillon de message. L'utilisateur choisit et envoie lui-même.

Deux cas :
- **Pas de `profil.yaml`** : parcours complet, étapes A à F, dans l'ordre.
- **`profil.yaml` existe** : demander (AskUserQuestion) ce qu'il veut : revoir ses critères
  (B et C seulement, en relisant le profil pour ne pas reposer ce qui est su), refaire une
  étape d'installation (D, E ou F), ou vérifier que tout marche (`--verifier`).

Avant chaque étape, dire en une ou deux phrases ce qu'on va faire et pourquoi.

## A. Prérequis et accord

1. `uname -s` : `Darwin` (macOS) ou `Linux`. Sous Windows (`MINGW`, `CYGWIN`, `MSYS` ou
   PowerShell), dire clairement que nest n'est pas pris en charge sur Windows (WSL : non
   testé) et s'arrêter là.
2. macOS : si le dépôt est dans `~/Documents`, `~/Desktop` ou `~/Downloads`, prévenir que
   macOS empêche la tâche planifiée d'y lancer un script, et proposer de le déplacer (ex.
   `~/nest`) avant de continuer.
3. `python3 --version` : 3.10 au minimum. Sinon, macOS : `brew install python` ou
   python.org ; Linux : le paquet `python3` de la distribution (et `python3-venv`).
4. Environnement : `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
5. Expliquer, puis demander un accord explicite (AskUserQuestion : « J'ai compris, on
   continue » / « Je préfère m'arrêter ») :
   - nest passe par une bibliothèque non officielle pour Leboncoin et, en option, par la
     session Gens de Confiance de l'utilisateur. Ça peut enfreindre les conditions
     d'utilisation de ces sites ; son compte est exposé ; la fréquence reste basse
     (10 min par défaut, jamais moins de 5). À ses risques, sans lien avec ces sites.
   - Coût : quelques dizaines de dollars par mois de Claude au plus fort d'une recherche
     dans une grande ville (détail dans le README), Google Routes reste en général
     dans le palier gratuit.

## B. Entretien

Le but n'est PAS de remplir une liste de critères. C'est de comprendre comment
l'utilisateur vit, pour en déduire ce qui compte dans un logement, y compris ce qu'il
n'aurait pas pensé à dire. La liste de critères est le produit final, pas le questionnaire.

### Posture

- Alterner questions ouvertes (comprendre) et choix fermés via AskUserQuestion (calibrer).
  S'il dicte à la voix, les questions ouvertes sont bienvenues.
- Partir du concret : situations vécues, journées réelles, logements passés. Jamais
  « quelle importance accordez-vous à la luminosité ? » mais « dans ton dernier logement,
  qu'est-ce qui te faisait du bien ou t'agaçait le soir en rentrant ? ».
- Un thème par tour, 3 à 5 questions au plus par tour. Reformuler en une ou deux phrases
  ce qu'on a compris avant de passer au thème suivant, pour qu'il corrige.
- Creuser quand une réponse cache un besoin (« je cuisine pas trop » → mange dehors ?
  quartier vivant ? ou plaque deux feux suffisante ?).
- Ne jamais inventer : un critère sans ancrage dans une réponse n'entre pas dans la liste.

### Déroulé (thèmes, dans cet ordre)

0. **L'essentiel** : prénom et âge ; ville et adresse du lieu de stage (puis
   `.venv/bin/python -m nest.geocoder "adresse"` et faire confirmer le libellé trouvé) ;
   heure d'arrivée ; dates d'arrivée et durée du séjour ; budget charges comprises ;
   studio, colocation ou les deux ; trajet maximal porte à porte (le SEUL critère
   éliminatoire) ; situation (études, stage ou alternance, revenus) ; garanties (garant,
   Visale, autre). Gens de Confiance : seulement s'il est membre ET cherche en
   Île-de-France (le site centre ses recherches sur Paris).
1. **Rythme de vie** : journée type, heures de départ et de retour, télétravail, soirées,
   week-ends (reste sur place ? rentre chez ses parents ? → proximité d'une gare ?),
   sport, sorties, invités.
2. **Logements passés** : ce qu'il a aimé, ce qu'il a détesté, ce qu'il ne revivrait pas.
   C'est souvent là que sortent les vrais critères (bruit, lumière, voisins, salle de bain).
3. **Le logement** : surface ressentie (pas juste les m²), espace de travail, cuisine,
   lit, rangement, lumière, étage et ascenseur, bruit et sommeil, chauffage, machine à
   laver, extérieur, animal éventuel.
4. **Le quartier** : ambiance recherchée, commerces, sport, vie nocturne, calme, sécurité,
   proximité d'amis ou de lieux réguliers.
5. **Le trajet, au-delà des minutes** : tolérance aux changements, marche (aime-t-il
   marcher ? → `poids_minute_marche`), vélo, moto avec parking (→ `moto: true`), trajets
   secondaires (gare, école, amis).
6. **Bail et budget** : durée réelle du séjour, bail mobilité, sous-location (pas d'APL),
   colocation (à quelles conditions), où mettre un euro de plus et où économiser.
7. **Arbitrages forcés** (AskUserQuestion, 4 à 6 dilemmes concrets, ex. « 15 m² à 15 min
   ou 22 m² à 30 min ? », « meublé moyen ou vide lumineux ? »). Ce sont eux qui fixent
   les poids relatifs : un critère qui gagne ses arbitrages monte en poids.

## C. Synthèse et `profil.yaml`

1. Proposer la liste longue (15 à 30 critères) groupée par famille, chaque critère avec
   son poids et une phrase « pourquoi ça compte pour toi », dans un tableau.
2. La faire valider par choix fermés (AskUserQuestion, multiSelect) : critères à monter,
   à baisser, à retirer.
3. Rédiger le modèle de premier message (`locataire.modele_message`) à partir de ses
   réponses, le lui montrer et le faire corriger. Demander s'il accepte que l'entreprise
   soit nommée (`nommer_employeur`, non par défaut) et ce qu'il ne veut jamais voir dans
   un message (`ne_jamais_mentionner`, ex. un animal).
4. Écrire `profil.yaml` en partant de `profil.example.yaml` (mêmes sections, mêmes clés,
   commentaires gardés) : `recherche`, `preferences` (portrait de 5 à 8 lignes),
   `locataire`, `criteres`, `arnaques`, `notif`, `mails`. Ne jamais modifier
   `profil.example.yaml`.

### Format des critères (contrat avec nest/scoring.py)

```yaml
criteres:
  - id: meuble                 # snake_case, unique
    libelle: Meublé            # 1 à 4 mots, affiché dans la notif
    famille: logement          # trajet | logement | quartier | bail | budget | vendeur
    poids: 3                   # 1 petit plus · 2 apprécié · 3 important · 4 très important · 5 essentiel
    pourquoi: "Arrive avec deux valises, pour un stage de 6 mois."
    consigne: "Attribut meublé ; sinon indices dans la description."   # lue par Claude

  # Critère mesuré par le code (pas par Claude) : interpolation linéaire entre paliers.
  - id: surface
    libelle: Surface confortable
    famille: logement
    poids: 3
    pourquoi: "..."
    auto: surface_m2           # voir la liste ci-dessous
    paliers: {12: 0, 18: 0.5, 25: 1}   # valeur → satisfaction (0 à 1)
    applique_a: [studio_t1]    # facultatif : limite le critère à un type de bien
```

Types `auto` : `trajet_min`, `changements`, `surface_m2`, `loyer_cc`, `distance_km` (avec
`point: {lat, lng}`, ex. une gare), `parc` (square à ~5 min, sans paliers), `escalade_km`
(salle de bloc la plus proche), `vendeur_particulier` (sans paliers), `compte_fiable`
(ancienneté du compte en mois).

Règles du score (à expliquer s'il demande) :
- Chaque critère vaut oui = 1, partiel = 0,5, inconnu = 0,5 (neutre), non = 0 ;
  les critères `auto` donnent directement une satisfaction entre 0 et 1.
- Score = 100 × Σ(poids × satisfaction) / Σ(poids). Un critère raté fait baisser
  l'annonce dans le classement, il ne l'élimine jamais.
- Seule la distance (`trajet_max_min`) élimine, avant le scoring.
- Formuler tous les critères positivement (« Pas en rez-de-chaussée », pas « RDC »).

## D. Les clés, dans `.env`

1. Si `.env` n'existe pas : `cp .env.example .env && chmod 600 .env`.
2. Expliquer comment obtenir chaque clé, une à la fois :
   - **Anthropic** (juge les annonces) : https://console.anthropic.com, créer un compte,
     ajouter du crédit prépayé dans Billing (5 $ suffisent pour démarrer), puis
     Settings → API keys → Create key.
   - **Google Routes** (vrais trajets) : https://console.cloud.google.com, créer un
     projet, lui associer un compte de facturation (exigé même dans le palier gratuit),
     activer « Routes API », puis Identifiants → Créer une clé API, et la restreindre à
     « Routes API ».
   - **ntfy** (notifs) : générer un nom de canal aléatoire avec
     `python3 -c "import secrets; print('nest-' + secrets.token_hex(8))"`, installer
     l'app ntfy (iOS ou Android) et s'abonner à ce canal. Le nom fait office de mot de
     passe : il reste secret.
   - Facultatif, **Gens de Confiance** : rappeler le risque pour son compte, puis :
     se connecter sur gensdeconfiance.com, outils de développement → Réseau → une
     requête vers le site → copier l'en-tête `Cookie` entier, entre guillemets.
   - Facultatif, **Gmail** et **Notion** : renvoyer aux docstrings de
     `nest/mails/gmail.py` et `nest/notion.py` ; proposer de passer, ça s'ajoute plus tard.
3. Recommander que l'utilisateur colle lui-même ses clés dans `.env` (macOS :
   `open -e .env` ; Linux : `nano .env`) : elles ne passent alors pas par la
   conversation. S'il préfère les coller ici, les écrire dans `.env` avec Edit.
   Dans les deux cas : ne jamais réafficher une clé, ne jamais faire `cat .env`.
4. Vérifier : `.venv/bin/python -m nest --verifier`, corriger jusqu'à n'avoir plus de ✗.
5. `.venv/bin/python -m nest --test-notif`, puis demander s'il a bien reçu la notif.

## E. La tâche planifiée

1. Montrer ce qui va être installé : `.venv/bin/python -m nest.planifier --afficher`.
2. Installer : `.venv/bin/python -m nest.planifier` (un passage toutes les 10 min ;
   `--intervalle 15` ou `30` s'il veut plus prudent ; jamais moins de 5).
3. Expliquer : sur Mac, ça tourne tant que la session est ouverte et le Mac éveillé ;
   les trous (nuits, veille) sont rattrapés au réveil. Sur Linux, c'est une ligne cron.
   Pour arrêter : `.venv/bin/python -m nest.planifier --retirer`.

## F. Premier passage à blanc

1. `.venv/bin/python -m nest --sec --max 3` : rien n'est envoyé ni enregistré, au plus
   3 annonces jugées (quelques centimes).
2. Commenter ce qui sort : une annonce, son score, ce qui coche et ce qui coince, le
   brouillon de message. Aucune candidate ? C'est normal s'il n'y a pas eu d'annonce
   proche récemment ; le dire plutôt que de baisser les critères.
3. Finir par un récapitulatif court : les notifs arrivent sur ntfy dès le prochain
   passage, le journal est dans `data/nest.log`, relancer `/entretien-recherche` pour
   ajuster, et le seuil de notif est `notif.score_min` (70 par défaut).
