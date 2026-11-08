# CinetPay PayIn / PayOut — Sandbox Think

**Branche d'implémentation :** `cinetpay-payin-payout-multi-numbers`  
**Portée :** environnement Sandbox uniquement. Aucune clé réelle ni valeur secrète n'est incluse dans ce dépôt.

## 1. Ce qui est intégré

### PayIn

- Crée un paiement CinetPay via le SDK Python V1.
- Les clés API restent uniquement dans les variables Render.
- Le numéro Mobile Money enregistré peut être sélectionné pour préremplir le réseau et le numéro transmis au guichet CinetPay.
- Think vérifie le jeton de notification puis demande le statut à CinetPay avant de créditer le wallet.
- Le retour du navigateur n'est jamais considéré comme une preuve de paiement.

### PayOut

- Utilise `client.transfer.create()` et `client.transfer.get_status()` du SDK Python V1 (API `/v1/transfer`).
- Le PayOut est volontairement verrouillé sur Sandbox : `APP_ENV=sandbox`, `CINETPAY_MODE=sandbox`, clé `sk_test_` et `CINETPAY_PAYOUT_SANDBOX_ENABLED=true` sont tous nécessaires.
- Un retrait réel n'est pas possible depuis cette implémentation. `payout_enabled` reste toujours `false`.
- Les fonds sont réservés avant l'appel provider. Ils restent réservés si le statut est `PENDING` ou inconnu.
- Seul un statut récupéré directement auprès de CinetPay peut confirmer le retrait ou libérer la réservation après un échec final.
- Une clé d'idempotence protège contre les doubles soumissions.

### Plusieurs numéros Mobile Money par utilisateur

- Un utilisateur peut enregistrer plusieurs numéros Airtel Money, Orange Money et M-Pesa.
- Il peut nommer chaque numéro, choisir un numéro principal et supprimer un numéro.
- Le numéro complet est chiffré en base avec Fernet. L'API renvoie uniquement un masque avec les quatre derniers chiffres.
- Les numéros enregistrés peuvent être choisis dans les formulaires PayIn et PayOut.
- L'historique de retrait n'expose pas le numéro complet.

## 2. Clés API Sandbox CinetPay

1. Connecte-toi au tableau de bord marchand CinetPay et vérifie que le bandeau indique bien **Sandbox** et que le service sélectionné est celui de CRYPT Entreprise / RDC.
2. Ouvre **Ressources → API et sécurité**. C'est également la rubrique que le support CinetPay a indiquée pour la liste blanche des adresses IP.
3. Cherche les identifiants de l'**API V1 / Sandbox** : une API Key de test commençant par `sk_test_` et le **mot de passe API** correspondant au pays CD.
4. Ne confonds pas ces identifiants avec les anciens paramètres d'intégration tels que Site ID / `cpm_site_id` du flux historique. Le backend actuel attend `api_key` + `api_password` du SDK V1.
5. Si le tableau de bord n'affiche pas une clé `sk_test_` et son mot de passe API V1, ne devine pas et ne remplace pas le mot de passe par le Site ID. Demande au support technique CinetPay de fournir/activer explicitement les identifiants **API V1 Sandbox pour le pays CD (RDC)**.

Le SDK officiel distingue Sandbox (`sk_test_`, API `https://api.cinetpay.net`) et Production (`sk_live_`, API `https://api.cinetpay.co`). Voir le [guide du SDK CinetPay](https://github.com/cinetpay/cinetpay-python) et la [documentation V1 des transferts](https://api.cinetpay.com/).

## 3. Variables Render

Dans Render → service `think-crypt-entreprise` → **Environment**, ajoute ou vérifie les valeurs suivantes :

```env
APP_ENV=sandbox
CINETPAY_MODE=sandbox
CINETPAY_ENABLED=true
CINETPAY_API_KEY_CD=sk_test_TA_CLE_SANDBOX
CINETPAY_API_PASSWORD_CD=TON_MOT_DE_PASSE_API_V1_SANDBOX
CINETPAY_CURRENCY=CDF
CINETPAY_PAYOUT_SANDBOX_ENABLED=true
CINETPAY_PAYOUT_ENABLED=false
CINETPAY_PRODUCTION_ENABLED=false
CINETPAY_MERCHANT_VERIFIED=false
THINK_PUBLIC_BASE_URL=https://think-crypt-entreprise.onrender.com
CINETPAY_NOTIFY_URL=https://think-crypt-entreprise.onrender.com/api/cinetpay/payin/notify
MOBILE_MONEY_ENCRYPTION_KEY=TA_CLE_FERNET
```

Remplace les deux premières valeurs secrètes par les valeurs exactes de ton espace Sandbox. N'ajoute aucune vraie clé à GitHub et ne les colle ni dans le frontend ni dans une conversation.

### Générer la clé de chiffrement Mobile Money

Sur ton ordinateur, avec Python installé, exécute :

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copie la chaîne générée dans Render comme valeur de `MOBILE_MONEY_ENCRYPTION_KEY`. Cette clé protège les numéros Mobile Money enregistrés dans la base. Conserve une copie de sauvegarde sécurisée : si elle est perdue ou remplacée sans migration de chiffrement, les numéros enregistrés ne pourront plus être déchiffrés. Génère une clé différente pour chaque environnement.

Render redémarre le service après l'enregistrement des variables.

## 4. Adresse IP autorisée — point bloquant possible

Le support CinetPay a précisé que les requêtes PayOut automatisées exigent une adresse IP source autorisée et que le code `2011 - NOT_ALLOWED` peut indiquer une IP non autorisée.

Dans Render, ouvre la page du service Think, puis **Connect → Outbound** pour consulter les plages IP sortantes attribuées au service. Render précise que les adresses de ces plages sont partagées entre plusieurs services d'une même région : ce n'est pas une IP unique réservée à Think. Demande d'abord à CinetPay s'ils acceptent une liste CIDR de Render ou s'ils exigent une IP unique.

Si CinetPay exige une IP fixe unique, Render propose des Dedicated Outbound IPs dans les workspaces Pro ou supérieurs. Chaque set comprend trois IPv4 statiques et coûte actuellement 100 USD/mois selon le changelog Render. **Ne souscris pas à cette option avant que CinetPay confirme que les plages partagées sont refusées et qu'une IP dédiée est bien nécessaire.**

Références :
- [Render — Outbound IP addresses](https://render.com/docs/outbound-ip-addresses)
- [Render — Dedicated IPs](https://render.com/docs/dedicated-ips)
- [Render — annonce des IP dédiées (19 mai 2026)](https://render.com/changelog/add-dedicated-outbound-ips-to-your-workspace)

## 5. Routes API ajoutées

Toutes ces routes exigent une authentification Think :

| Méthode | Route | Utilité |
|---|---|---|
| GET | `/api/mobile-money/accounts` | Lister les numéros enregistrés (masqués) |
| POST | `/api/mobile-money/accounts` | Enregistrer un numéro |
| POST | `/api/mobile-money/accounts/{id}/default` | Définir le numéro principal |
| DELETE | `/api/mobile-money/accounts/{id}` | Désactiver un numéro |
| POST | `/api/withdrawals` | Créer un PayOut Sandbox |
| GET | `/api/withdrawals` | Historique des retraits |
| POST | `/api/withdrawals/{public_id}/refresh` | Vérifier à nouveau le statut provider |
| GET/POST | `/api/cinetpay/payout/notify` | Ping et notification PayOut |

Le PayIn existant reste :
- `POST /api/payments/create`
- `POST /api/cinetpay/payin/notify`
- `POST /api/payments/{public_id}/refresh`

## 6. Parcours de test à faire ensemble

### Préparation
- [ ] Déployer cette branche sur un service Sandbox (ou fusionner la PR après revue).
- [ ] Ajouter les variables Render ci-dessus.
- [ ] Vérifier `GET /health` : `ok=true`, `environment=sandbox`, base connectée.
- [ ] Se connecter à Think et vérifier `GET /api/cinetpay/status` : PayIn actif et `payout_sandbox_enabled=true`.
- [ ] Enregistrer deux ou trois numéros de test autorisés par CinetPay. Vérifier que l'interface n'affiche que les quatre derniers chiffres.

### PayIn
- [ ] Effectuer un dépôt test CDF depuis le guichet Sandbox.
- [ ] Vérifier que le paiement est crédité uniquement après confirmation du statut CinetPay.
- [ ] Rejouer le statut/notification : une seule écriture de ledger et un seul crédit.

### PayOut
- [ ] Choisir un numéro Mobile Money sauvegardé et lancer un petit transfert test (au moins 100 CDF).
- [ ] Vérifier la référence Think et la référence CinetPay.
- [ ] Tester un résultat `SUCCESS` : le montant réservé est consommé et le statut devient `payout_success`.
- [ ] Tester `PENDING` : le montant reste réservé ; utiliser le bouton **Vérifier** pour relire le statut.
- [ ] Tester `FAILED` : la réservation est libérée une seule fois et le montant revient au disponible.
- [ ] Rejouer une même requête avec la même clé d'idempotence : aucun deuxième transfert.
- [ ] Si erreur `2011 NOT_ALLOWED`, vérifier l'IP sortante/allowlist avec CinetPay avant de conclure à un défaut du code.

**Ne pas utiliser un PIN ou un compte Mobile Money réel sur un parcours qui ne se présente pas clairement comme Sandbox.** Suivre exclusivement les instructions de simulation fournies par CinetPay. Aucun transfert réel ne doit être tenté.

## 7. Limites et points à confirmer

- Le PayOut est configuré pour le CDF et les trois réseaux confirmés pour le flux RDC : Airtel, Orange et M-Pesa. Africell n'est pas exposé en PayOut tant que CinetPay ne confirme pas cette méthode.
- Le SDK Python V1 installé valide les montants de transfert entre 100 et 1 500 000 unités. Le minimum 100 CDF vient de la réponse du support ; le maximum 1 500 000 est une limite du SDK générique, pas une confirmation du plafond contractuel RDC.
- Les frais spécifiques de PayOut, le montant net payé au bénéficiaire, les délais et les plafonds contractuels doivent être confirmés par CinetPay. Le code conserve la valeur de frais renvoyée par le fournisseur, mais ne facture pas de frais Think supplémentaires.
- Le service ne réalise pas de règlement marchand (reversement des encaissements vers le compte bancaire/Wallet de CRYPT). C'est distinct du PayOut vers un utilisateur.
- Les statuts en attente/inconnus restent réservés : ne pas refaire un transfert manuellement avant rapprochement avec CinetPay.
- Cette livraison ne permet aucun PayOut de production. La conformité KYC, l'autorisation PayOut et la validation de l'IP restent indispensables.

---
**Ne pas déployer les identifiants live dans cette configuration.** La bascule production fera l'objet d'une revue séparée du code, des autorisations, des paramètres et de la comptabilité.
