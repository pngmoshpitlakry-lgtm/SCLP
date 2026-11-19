# Scanner memecoins Solana → Discord

Un scan toutes les ~5 minutes, 24h/24, sans que ton PC soit allumé.
Tu reçois sur Discord :

- **Alertes instantanées** : 🚀 Momentum, ⚡ Pic de volume, 🆕 Nouveau runner, avec MCap, liquidité, achats/ventes, vérification RugCheck et liens directs Axiom / Pump.fun.
- **Alertes de sortie** 📉 : un token déjà alerté qui perd 35 % depuis son plus haut.
- **Résumé du matin** (ce qui s'est passé pendant la nuit) et **résumé du soir** (pendant ta journée de travail), avec la performance de chaque token depuis son alerte.

## Installation (environ 10 minutes)

### 1. Créer le webhook Discord
1. Sur ton serveur Discord, crée un salon `#alertes-memecoins`.
2. Clic droit sur le salon → **Modifier le salon** → **Intégrations** → **Webhooks** → **Nouveau webhook**.
3. Clique sur **Copier l'URL du webhook** et garde-la de côté. Ne la partage jamais : quiconque la possède peut écrire dans ton salon.

### 2. Créer le dépôt GitHub
1. Crée un compte sur github.com si tu n'en as pas.
2. **New repository** → nom au choix (ex. `scanner`) → **Public** (obligatoire : c'est gratuit en illimité pour un dépôt public, alors qu'un dépôt privé ne couvre pas un scan toutes les 5 minutes) → **Create repository**.
3. **Add file → Upload files** : dépose `scanner.py` et `README.md`, puis **Commit changes**.
4. **Add file → Create new file**. Dans le nom du fichier, tape exactement `.github/workflows/scanner.yml` (les `/` créent les dossiers). Colle le contenu de `scanner.yml`, puis **Commit changes**.

Le code est visible publiquement, mais pas ton webhook : il va dans un secret, étape suivante.

### 3. Ajouter ton webhook en secret
Dépôt → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- Name : `DISCORD_WEBHOOK_URL`
- Secret : l'URL copiée à l'étape 1

### 4. Tester
Onglet **Actions** → clique sur **scanner** à gauche (active les workflows si GitHub le demande) → **Run workflow** → mode `test` → **Run workflow**.
Un message « ✅ Scanner connecté » doit arriver dans Discord. Ensuite, relance avec le mode `scan` pour un premier vrai scan.

À partir de là, tout tourne seul.

## Réglages
Tout en haut de `scanner.py`, dans le bloc `CFG` : seuils de liquidité, de volume, de variation, durée anti-doublon, etc.
Pour modifier : clique sur le fichier dans GitHub → icône crayon → **Commit changes**.
Trop d'alertes ? Monte `mom_volume_h1_usd` et `min_liquidity_usd`. Pas assez ? Baisse-les.

## Limites à connaître
- **GitHub retarde parfois** les exécutions planifiées de quelques minutes (surtout aux heures de forte charge). Ce n'est pas du temps réel : pour le sniping à la seconde, il faut des bots dédiés.
- **Dépôt inactif** : GitHub désactive les tâches planifiées d'un dépôt public sans activité pendant 60 jours. Si les alertes s'arrêtent, réactive le workflow dans l'onglet Actions (ou modifie un fichier).
- **Heure d'été** : les résumés sont calés sur l'heure d'hiver. En été, ils arrivent une heure plus tard (modifiable dans les lignes `cron` de `scanner.yml`, en UTC).
- **Aucune garantie de gain.** Un signal n'est pas une recommandation d'achat : un token qui pompe peut être un piège, et RugCheck n'est pas infaillible. Ce n'est pas un conseil financier.
