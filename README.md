<div align="center">

# Dictée

**Parle. Ça s'écrit.**

Maintiens <kbd>Ctrl</kbd> + <kbd>Espace</kbd> et dicte dans n'importe quel logiciel.<br>
Tout se passe sur ton PC.

[Télécharger](https://github.com/FakerIRL/dictee/archive/refs/heads/main.zip) · [Site](https://fakerirl.github.io/dictee/) · [Licence MIT](LICENSE)

<br>

![Aperçu de la bulle de dictée](docs/apercu.png)

</div>

## Installation

Windows 10 ou 11. Trois minutes.

1. **Python** : installe-le depuis [python.org](https://www.python.org/downloads/) en cochant « Add python.exe to PATH ».
2. **Dictée** : télécharge le ZIP et décompresse-le où tu veux.
3. **C'est prêt** : double-clique sur `installer.bat`. L'icône apparaît près de l'horloge.

Le modèle de reconnaissance (environ 500 Mo) est téléchargé une seule fois, à la première dictée.
Pour relancer l'appli plus tard : `lancer.bat`, ou **Lancer au démarrage** dans le menu de l'icône.

## Utilisation

| Action | Comment |
|---|---|
| Dicter | Maintenir <kbd>Ctrl</kbd> + <kbd>Espace</kbd>, parler, relâcher |
| Activer, couper, choisir le micro | Clic droit sur l'icône près de l'horloge |

L'icône est verte quand l'appli est prête, rouge pendant l'écoute, orange pendant la transcription et grise quand elle est coupée.

## Réglages

Fichier `%APPDATA%\Dictee\config.json`. Relance l'appli après une modification.

```json
{
  "modele": "small",
  "langue": "fr",
  "vocabulaire": "Discord, Kubernetes, Jean-Baptiste",
  "garder_modele_secondes": 60
}
```

| Clé | Rôle |
|---|---|
| `modele` | `base`, `small`, `medium` ou `large-v3-turbo`. Plus le modèle est gros, plus il est précis, mais plus il est lent. |
| `langue` | Langue parlée : `fr`, `en`, `es`… |
| `vocabulaire` | Mots rares à bien orthographier. |
| `garder_modele_secondes` | Temps pendant lequel le modèle reste en mémoire après une dictée. |

## Bon à savoir

- **Jeux en plein écran** : la bulle ne s'affiche pas par-dessus un jeu en plein écran exclusif. Passe le jeu en fenêtré sans bordure. La dictée fonctionne dans les deux cas.
- **Captures et OBS** : la bulle est volontairement absente des captures d'écran, ce qui permet l'effet de verre.
- **Problème** : les erreurs sont notées dans `%APPDATA%\Dictee\erreurs.log`.
- **Désinstaller** : décoche le lancement au démarrage, quitte l'appli, puis supprime le dossier du projet, `%APPDATA%\Dictee` et `%USERPROFILE%\.cache\huggingface`.

## Crédits

Reconnaissance vocale par [Whisper](https://github.com/openai/whisper) (OpenAI), via [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
