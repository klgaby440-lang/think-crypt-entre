# Intégration CinetPay — Think (RDC)

État de cette intégration : **PayIn Sandbox uniquement**.

Le compte marchand CRYPT Entreprise RDC est encore en attente de vérification et l'activation PayOut n'a pas été confirmée. En conséquence :

- les clés `sk_test_` sont obligatoires pour tester PayIn ;
- la production est verrouillée ;
- PayOut reste désactivé ; aucune API de transfert sortant CinetPay n'est appelée ;
- les transferts entre deux portefeuilles Think restent internes à Think ;
- les demandes de retrait actuelles restent des simulations Sandbox.

## SDK et environnement

Le backend utilise le SDK officiel CinetPay v1 :

```text
cinetpay-python==0.1.0
```

Le SDK reconnaît :
- `sk_test_...` → `https://api.cinetpay.net` (Sandbox)
- `sk_live_...` → `https://api.cinetpay.co` (production)

Le pays configuré est `CD` (RDC), en CDF. Les méthodes référencées par le SDK pour la RDC sont `OM_CD`, `AIRTEL_CD`, `MPESA_CD` et `AFRICELL_CD`.

## Variables Render — Sandbox

Configurer les variables suivantes dans le service **backend Think** sur Render. Les valeurs secrètes doivent rester dans Render ; ne pas les ajouter au dépôt et ne pas les mettre dans le JavaScript frontend.

| Variable | Valeur |
|---|---|
| `APP_ENV` | `sandbox` |
| `CINETPAY_ENABLED` | `true` |
| `CINETPAY_MODE` | `sandbox` |
| `CINETPAY_API_KEY_CD` | Clé Sandbox fournie par CinetPay, préfixée par `sk_test_` |
| `CINETPAY_API_PASSWORD_CD` | Mot de passe API Sandbox fourni par CinetPay |
| `CINETPAY_CURRENCY` | `CDF` |
| `CINETPAY_MERCHANT_VERIFIED` | `false` |
| `CINETPAY_PRODUCTION_ENABLED` | `false` |
| `CINETPAY_PAYOUT_ENABLED` | `false` |
| `THINK_PUBLIC_BASE_URL` | `https://think-crypt-entreprise.onrender.com` |
| `CINETPAY_NOTIFY_URL` | `https://think-crypt-entreprise.onrender.com/api/cinetpay/payin/notify` |

Conserver également les variables existantes `JWT_SECRET`, `THINK_DATABASE_URL` et `THINK_DATABASE_NAME=think_db`.

Le backend utilise l'authentification du SDK v1 avec le couple API Key / API Password du pays CD. Ne pas ajouter des valeurs de l'ancienne API Checkout v2 (`site_id`, `apikey` ou ancien secret HMAC) à la place des identifiants v1.

## Parcours PayIn

1. L'utilisateur connecté ouvre **Portefeuille → Solde → Ajouter de l'argent**.
2. Il choisit un montant en CDF et, facultativement, un opérateur.
3. Think enregistre d'abord la transaction en base, puis appelle `client.payment.initialize(..., "CD")`.
4. Le backend renvoie une URL de paiement CinetPay et le navigateur redirige l'utilisateur vers le guichet.
5. Le guichet appelle `/api/cinetpay/payin/notify` quand le statut évolue.
6. Think valide le `notify_token` enregistré et demande le statut à l'API CinetPay.
7. Seul le statut vérifié `SUCCESS` crédite le portefeuille, dans une seule transaction PostgreSQL avec une écriture de grand livre unique.

Le retour navigateur `/api/cinetpay/payin/return` n'est qu'un retour d'interface : **il ne crédite jamais le portefeuille**.

Les notifications répétées sont acceptées sans double crédit. Les clés `Idempotency-Key` empêchent une seconde initialisation pour une même demande.

## Règles de montant actuelles

Selon les règles du SDK v1 utilisé dans cette intégration :

- devise : CDF ;
- montant entier ;
- minimum : 100 CDF ;
- maximum : 2 500 000 CDF ;
- montant multiple de 5.

Le nom et l'adresse e-mail du compte Think sont envoyés au guichet. Si le profil n'a pas de prénom et de nom séparés, Think utilise les parties du nom complet ; un nom complet avec au moins deux éléments est donc nécessaire.

## Vérification et test

1. Ajouter les variables Sandbox ci-dessus dans Render et déclencher un déploiement du backend.
2. Se connecter à Think.
3. Ouvrir `GET /api/cinetpay/status` avec une session authentifiée. Attendu : `payin_enabled: true`, `mode: "sandbox"`, `currency: "CDF"` et `payout_enabled: false`.
4. Dans l'application, aller dans **Portefeuille → Solde → Ajouter de l'argent**.
5. Essayer une petite valeur de test valide, par exemple 1 000 CDF, avec le moyen souhaité.
6. Utiliser exclusivement les données de test et les moyens de paiement indiqués par CinetPay pour son environnement Sandbox. Ne pas saisir un numéro réel pour simuler un paiement.
7. Après confirmation par CinetPay, vérifier que le paiement apparaît comme `accepted`, que le portefeuille est crédité une seule fois et qu'une ligne `CNPIN-<référence>` existe dans le grand livre.

Le crédit fictif local `/api/wallet/sandbox-credit` reste disponible séparément : il ne contacte pas CinetPay et ne doit pas être confondu avec un paiement PayIn.

Si `payin_enabled` est faux, vérifier d'abord `CINETPAY_ENABLED`, le préfixe `sk_test_`, le mot de passe API et `APP_ENV=sandbox`.

Si le backend affiche `initialization_unknown`, ne pas refaire aveuglément le dépôt avec une nouvelle clé : consulter la référence Think dans l'historique et utiliser l'action de vérification du statut avant toute nouvelle tentative.

## Production et PayOut : non activés

Aucune transaction réelle ne peut être déclenchée par cette configuration. L'ouverture de la production exigera au minimum :

- compte marchand vérifié par CinetPay ;
- clés de production `sk_live_` ;
- validation explicite de la configuration de production ;
- tests et revue de sécurité.

PayOut nécessite une validation distincte de CinetPay pour le compte CRYPT et une intégration dédiée. Ne pas activer PayOut ni modifier les variables Sandbox pour tenter de lancer des sorties réelles.
