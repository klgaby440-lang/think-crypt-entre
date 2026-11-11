# CRYPT ID — identité universelle des produits

## Identifiant public

Chaque compte central Think reçoit un identifiant CRYPT immuable au format :

`INK_` + exactement 15 caractères alphanumériques majuscules (alphabet sans caractères visuellement ambigus), par exemple `INK_A7K9M2Q8X4P6R1T`.

L'INK ID est unique, permanent et ne remplace pas l'ancien `think_id` (`THK-...`). Le Think ID est conservé pour la compatibilité des transferts et intégrations historiques.

## Création et migration

- À l'inscription, Think génère l'INK ID de manière aléatoire et l'enregistre avec une contrainte d'unicité en base.
- Au démarrage, Think ajoute la colonne `crypt_id` si elle manque, attribue un INK ID à chaque compte historique qui n'en possède pas, crée l'index unique et rend la colonne obligatoire.
- Cette migration est idempotente : elle peut être exécutée à chaque démarrage sans réattribuer les identifiants déjà présents.
- L'API `/api/me` et les réponses de création de compte renvoient `crypt_id`.

## Connexion universelle

`POST /api/auth/login` accepte toujours le même schéma JSON pour ne pas casser les clients existants :

```json
{
  "email": "INK_A7K9M2Q8X4P6R1T",
  "password": "mot-de-passe"
}
```

Le champ historique `email` contient soit l'adresse e-mail, soit l'INK ID. Les clients peuvent donc migrer leur formulaire sans nouvelle route. Les mots de passe restent vérifiés par Think ; un INK ID seul ne donne jamais accès au compte.

Pour les applications qui souhaitent ouvrir une session Llink, le parcours SSO actuel reste disponible :

1. L'application envoie l'INK ID et le mot de passe à `POST /api/auth/login`.
2. Après authentification, elle appelle `POST /api/auth/llink/exchange` avec le jeton Think.
3. Think signe un ticket de deux minutes, à usage unique et limité à l'audience Llink. Le ticket contient `crypt_id` et le `think_id` historique.
4. Llink vérifie signature, audience, expiration, identifiant, puis consomme le `jti` anti-rejeu avant d'ouvrir sa session Firebase.

Le `crypt_id` est propagé en claim Firebase `crypt_id`. Pour préserver les conversations, pièces jointes et données Firebase déjà créées, l'UID Firebase historique `think_<think_id>` n'est pas modifié.

## Séparation des données des produits

L'INK ID est l'identité commune ; il ne fusionne pas les espaces de données, les rôles, les droits ni les portefeuilles des produits. Chaque produit doit encore autoriser explicitement l'utilisateur et associer son profil local à l'identité CRYPT. Une application qui n'a pas encore intégré l'échange SSO et l'association de ses comptes locaux ne doit pas être présentée comme connectée à l'identité universelle.

## Sécurité

- Ne jamais utiliser l'adresse e-mail comme clé d'identité centrale.
- Ne jamais considérer l'INK ID seul comme un secret ou un moyen d'authentification.
- Ne pas fusionner des comptes Google historiques et Think sur la seule égalité d'adresse e-mail.
- Ne pas modifier les secrets partagés dans Git. Les secrets SSO restent configurés côté Render.
- Les clients ne transmettent jamais le montant d'un paiement comme preuve d'autorisation ; les produits conservent leur facturation et leurs permissions séparées.
