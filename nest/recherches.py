"""Plusieurs recherches sur un seul profil (23/11/2026).

La recherche principale est le profil tel quel. Chaque entrée de
`profil.yaml > autres_recherches` reprend tout le profil (bureaux, trajet,
préférences, critères, dossier) et ne remplace que ce qu'elle redéfinit :

- `recherche` : clés fusionnées dans `recherche` (types, budget...) ;
- `preferences_en_plus` : ajouté à la fin des préférences ;
- `criteres_retires` / `criteres_en_plus` : critères enlevés ou ajoutés ;
- `groupe` : recherche à plusieurs (budget selon le nombre de chambres, message
  de groupe) ;
- `prefixe` : marque en tête du titre des notifs, ex. « 👥 ».

Les annonces sont suivies à part pour chaque recherche (voir `base.cle_recherche`) :
un T3 écarté par la recherche principale pour son loyer reste jugé par la coloc.

Exemple :
    >>> for rid, p in lister(charger_profil()):
    ...     print(rid, p["recherche"]["types"], p["recherche"]["budget_max_cc"])
    principale ['studio_t1', 'colocation'] 1100
    coloc_amis ['grand_logement', 'colocation'] 3000
"""

import copy

PRINCIPALE = "principale"


def _deriver(profil: dict, autre: dict) -> dict:
    """Profil effectif d'une autre recherche : copie du principal, puis remplacements."""
    p = copy.deepcopy({k: v for k, v in profil.items() if k != "autres_recherches"})
    p["recherche"].update(copy.deepcopy(autre.get("recherche", {})))
    if en_plus := autre.get("preferences_en_plus"):
        p["preferences"] = p["preferences"].rstrip() + "\n\n" + en_plus.strip() + "\n"
    retires = set(autre.get("criteres_retires", []))
    p["criteres"] = ([c for c in p.get("criteres", []) if c["id"] not in retires]
                     + copy.deepcopy(autre.get("criteres_en_plus", [])))
    if "groupe" in autre:
        p["groupe"] = copy.deepcopy(autre["groupe"])
    p["id_recherche"] = autre["id"]
    p["nom_recherche"] = autre.get("nom", autre["id"])
    p["prefixe"] = autre.get("prefixe")
    return p


def lister(profil: dict) -> list[tuple[str, dict]]:
    """[(id, profil effectif)], la recherche principale d'abord."""
    principal = {k: v for k, v in profil.items() if k != "autres_recherches"}
    principal = {**principal, "id_recherche": PRINCIPALE, "nom_recherche": "Recherche principale",
                 "prefixe": None}
    return [(PRINCIPALE, principal)] + [(a["id"], _deriver(profil, a))
                                        for a in profil.get("autres_recherches") or []]


def plafond_groupe(chambres: int, groupe: dict) -> float | None:
    """Loyer total maximal pour ce nombre de chambres, ex. {2: 2000, 3: 3000}.

    Au-delà de la plus grande clé, c'est son plafond qui vaut (4 chambres = plafond de 3).
    None si le nombre de chambres est sous le minimum.
    """
    table = {int(k): float(v) for k, v in (groupe.get("budget_par_chambres") or {}).items()}
    eligibles = [k for k in table if k <= chambres]
    return table[max(eligibles)] if eligibles else None
