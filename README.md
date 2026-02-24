# developper_ai_local

Application Python/Tkinter pour Windows qui pilote un modèle d'IA **local** (via `ollama`) afin de générer un projet logiciel de bout en bout à partir d'une description.

## Fonctionnalités
- Interface Tkinter (Windows/Linux) pour lancer un cycle autonome de développement.
- Exécution locale sans API externe.
- Utilisation par défaut d'un modèle léger compatible petites configurations RAM (`tinyllama:latest`).
- Téléchargement automatique du modèle s'il est absent localement.
- Actions pilotées par l'IA:
  - création de dossiers/fichiers,
  - lecture/modification de code,
  - exécution de commandes build/test,
  - corrections itératives selon les erreurs rencontrées.
- Support des cibles Python ou C++ (CMake côté C++).
- Objectif configurable en volume de code (LOC) et nombre d'itérations.

## Fichier principal
- `local_ai_dev_studio.py`

## Prérequis
1. Python 3.10+
2. Ollama installé localement
3. Ollama peut télécharger automatiquement un modèle léger (par défaut `tinyllama:latest`, ~1 Go ou moins selon la plateforme) au premier lancement.

## Lancement
```bash
python local_ai_dev_studio.py
```

## Notes importantes
- Générer 250k à 1M de lignes est techniquement coûteux (temps, RAM, disque). Le programme permet de configurer cette cible, mais la réussite dépend du matériel et du modèle local.
- L'agent exécute des commandes shell dans le dossier projet: utiliser un répertoire dédié.
