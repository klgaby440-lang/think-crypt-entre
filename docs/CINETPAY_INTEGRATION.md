# Intégration CinetPay — état Think (Sandbox)

Ce document décrit la configuration actuellement prévue pour Think. Il ne déclenche aucun paiement ni transfert externe.

## État du compte communiqué

- Environnement Sandbox : disponible côté tableau de bord.
- Vérification du compte marchand : `pending`.
- Activation PayOut : `unknown`.

## Décision de sécurité

1. **PayIn Sandbox** peut être activé avec les identifiants de test CinetPay, sous réserve que le compte API et les URLs de notification soient correctement configurés. La vérification marchande en attente bloque la production, pas les essais Sandbox.
2. **Transferts internes Think** restent des écritures PostgreSQL entre deux portefeuilles Think. En Sandbox, ils ne déplacent aucun fonds réels.
3. **Retraits vers Mobile Money** restent en mode simulation. Le backend peut réserver un montant Think et enregistrer une demande, mais ne doit appeler aucune API de transfert CinetPay.
4. **Production PayIn et PayOut** restent bloquées jusqu'à confirmation écrite de CinetPay, configuration de clés live, revue de sécurité et activation séparée.

## Variables Render — Sandbox uniquement

Configurer les variables suivantes dans le service Think (ne jamais commiter les vraies valeurs) :

```env
APP_ENV=sandbox
CINETPAY_MODE=sandbox
CINETPAY_SANDBOX_STATUS=yes
CINETPAY_ENABLED=false
CINETPAY_API_KEY_CD=
CINETPAY_API_PASSWORD_CD=
CINETPAY_CURRENCY=CDF
CINETPAY_MERCHANT_VERIFICATION_STATUS=pending
CINETPAY_PAYOUT_STATUS=unknown
CINETPAY_PAYOUT_ENABLED=false
CINETPAY_PRODUCTION_ENABLED=false
THINK_PUBLIC_BASE_URL=https://think-crypt-entreprise.onrender.com
CINETPAY_NOTIFY_URL=https://think-crypt-entreprise.onrender.com/api/cinetpay/payin/notify
```

Après avoir collé les vraies clés Sandbox dans Render, vérifier qu'elles sont de test (`sk_test_`) et que le mot de passe correspond bien à l'API Pays CD. Puis activer `CINETPAY_ENABLED=true`. Ne jamais mettre une clé live dans une configuration Sandbox.

Le statut `/api/cinetpay/status` expose les états marchands, PayOut, environnement et les blocages sans exposer les clés ou les mots de passe.

## Point critique : deux interfaces PayOut à ne pas confondre

La documentation CinetPay « API Transfert d'argent v1.1.0 » décrit l'interface historique :
- authentification via `client.cinetpay.com/v1/auth/login` avec `apikey` et mot de passe API ;
- ajout préalable du numéro dans les contacts CinetPay ;
- ordre de transfert par `/v1/transfer/money/send/contact` ;
- confirmation marchand par e-mail, sauf accord CinetPay et autorisation de l'adresse IP publique ;
- vérification du résultat auprès de CinetPay ; le webhook seul ne suffit pas.

Cette documentation indique pour la RDC (CDF) une fourchette de 1 000 à 5 000 000 CDF et les réseaux Orange Money, M-Pesa et Airtel. Les lignes RDC USD sont indiquées « In process ». La présence de l'API PayIn pour Africell ne prouve donc pas que le PayOut Africell est activé.

Le SDK Python déclaré dans `requirements.txt` expose également `client.transfer.create()`, `client.transfer.get_status()` et `client.balance.get()`, sur une autre base API (`api.cinetpay.net` en Sandbox / `api.cinetpay.co` en production). Son contrat de transfert générique indique des limites différentes (500 à 1 500 000). Il ne faut pas supposer que ce contrat et l'interface historique `client.cinetpay.com` sont interchangeables pour le compte RDC. CinetPay doit confirmer quel produit PayOut et quelles routes sont activés pour ce compte précis avant de choisir l'adaptateur.

## Conditions avant d'activer un vrai retrait

Obtenir de CinetPay, par écrit :
- statut marchand passé à vérifié ;
- PayOut explicitement activé pour la RDC et les réseaux voulus ;
- confirmation que l'API / le produit et les identifiants Sandbox permettent réellement des essais de retrait ;
- barèmes RDC applicables : devise, montants minimum/maximum, frais et réseaux autorisés ;
- adresse IP sortante fixe de Render à autoriser, si le flux automatique l'exige ;
- règles de confirmation marchand et de traitement des transferts.

Après validation, implémenter l'adaptateur PayOut identifié (ne pas basculer aveuglément du SDK moderne vers l'API historique), stockage des références provider et jetons de notification côté serveur, route de notification GET/POST, vérification de statut auprès de CinetPay, idempotence, rapprochement et gestion atomique des réservations. Un montant réservé ne sera libéré ou débité définitivement qu'après un statut CinetPay vérifié : succès final, rejet final ou état transitoire.

## Tests réalisables maintenant

- Créer deux comptes Think et effectuer un transfert interne Sandbox.
- Ajouter des crédits fictifs via `/api/wallet/sandbox-credit`.
- Tester PayIn via `/api/payments/create` lorsque les identifiants Sandbox sont en place et que `CINETPAY_ENABLED=true`.
- Vérifier les notifications et le rapprochement PayIn.
- Enregistrer un retrait simulé et vérifier la réservation du solde. Aucun Mobile Money ne doit être crédité.

Ne pas considérer une redirection de retour navigateur comme preuve de paiement : le serveur doit interroger CinetPay et confirmer l'état avant de créditer un portefeuille.


## Étape 1 — PayIn : durcissement du parcours

Le parcours PayIn utilise le même backend et les mêmes routes en Sandbox et en production. Seuls l'environnement, les clés et les autorisations changent. La production reste verrouillée dans le code et dans la configuration tant que la conformité n'est pas validée.

### Parcours nominal

1. L'utilisateur authentifié demande un dépôt CDF.
2. Think valide la devise, le montant, le réseau choisi et la clé d'idempotence.
3. Think valide l'URL de notification HTTPS avant de créer une transaction.
4. Think crée une référence marchande unique et initialise le paiement via le SDK CinetPay.
5. Think vérifie que CinetPay a retourné la même référence marchande, une URL checkout sur le domaine attendu pour l'environnement, un identifiant de transaction et un jeton de notification.
6. Le navigateur est redirigé vers le checkout CinetPay.
7. La notification reçue est authentifiée avec le jeton conservé côté serveur. Think interroge ensuite CinetPay directement : le contenu de la notification ou le retour navigateur ne peut pas créditer le portefeuille à lui seul.
8. Après confirmation du statut `SUCCESS` et vérification des références, Think crédite le portefeuille et inscrit une entrée de ledger avec une référence unique. Les notifications répétées ne doivent pas créditer deux fois.

### Durcissements ajoutés dans cette étape

- Validation stricte de `CINETPAY_NOTIFY_URL` : HTTPS obligatoire, hôte présent, absence d'identifiants intégrés et longueur maximale compatible avec le SDK.
- La configuration de notification est validée avant de persister une transaction, afin d'éviter les transactions bloquées à l'état `initializing` à cause d'une URL incorrecte.
- Contrôle de la référence marchande renvoyée par CinetPay lors de l'initialisation. Une incohérence place la transaction en `initialization_unknown` et interdit une nouvelle initialisation aveugle.
- Gestion explicite d'une collision concurrente sur une même clé d'idempotence : Think récupère la demande existante ou répond avec un conflit sûr, sans créer volontairement un deuxième paiement.
- Les secrets de notification restent exclus des réponses API utilisateur.

### Matrice de validation avant production

- [ ] PayIn Sandbox nominal : checkout ouvert, paiement test terminé, statut confirmé par interrogation serveur.
- [ ] Paiement refusé : aucun crédit du portefeuille.
- [ ] Notification invalide ou jeton incorrect : rejet HTTP, aucun crédit.
- [ ] Référence marchande ou identifiant CinetPay incohérent : rejet, aucun crédit.
- [ ] Notification dupliquée : une seule écriture de ledger et un seul crédit.
- [ ] Deux requêtes simultanées avec la même clé d'idempotence : une seule transaction marchande.
- [ ] Retour navigateur « success » sans confirmation CinetPay : aucun crédit.
- [ ] Indisponibilité temporaire de CinetPay : statut conservé et vérification réessayable sans recréer aveuglément le paiement.
- [ ] Vérifier les URL de notification et de retour en HTTPS sur le domaine Render.
- [ ] Rejouer tous les tests avec les clés et paramètres de production dans un environnement contrôlé avant ouverture aux utilisateurs.

Les tests de la matrice doivent être exécutés avec de vraies clés Sandbox dans Render. Aucun résultat de test fournisseur n'est supposé ou déclaré réussi par la seule modification du code.
