"""Format d'annonce commun à toutes les sources.

Chaque connecteur (Leboncoin, Bien'ici, PAP...) convertit ses données vers
cette classe : le reste du pipeline n'a jamais à connaître le site d'origine.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class Annonce:
    """Une annonce de location, normalisée."""

    source: str                 # "leboncoin", "bienici"...
    id_source: str              # identifiant sur le site d'origine
    url: str
    titre: str
    description: str
    type_bien: str              # "studio_t1", "colocation" ou "autre"
    loyer_cc: float | None      # charges comprises
    loyer_hc: float | None
    charges: float | None
    surface_m2: float | None
    pieces: int | None
    meuble: bool | None
    lat: float | None
    lng: float | None
    position_precise: bool      # False = centre de la commune / de l'arrondissement
    lieu: str                   # libellé lisible, ex. "Paris 75011"
    publiee_le: str             # "AAAA-MM-JJ HH:MM:SS", heure de Paris
    disponible_le: str | None   # tel que donné par le site, ex. "01/2027"
    photos: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)  # étage, DPE, ascenseur...
    vendeur_id: str | None = None                # compte Leboncoin de l'annonceur

    @property
    def cle(self) -> str:
        """Identifiant unique toutes sources confondues (sert au dédoublonnage)."""
        return f"{self.source}:{self.id_source}"

    def to_dict(self) -> dict:
        """Version sérialisable (stockage SQLite, envoi à Claude)."""
        return asdict(self)
