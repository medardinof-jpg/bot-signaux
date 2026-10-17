"""Bot Telegram de signaux (Apple, Tesla, EUR/USD) pour GitHub Actions.

Le script s'exécute UNE fois, puis s'arrête (GitHub le relance toutes les 15 min) :
1. il lit les messages reçus (/start, /signaux),
2. il analyse le marché (RSI, MACD, moyennes mobiles 20 et 50 jours),
3. il t'écrit quand un signal change.

Le token n'est pas dans le code : il vient du secret BOT_TOKEN de GitHub.
"""
import json
import os
import sys

import requests

SYMBOLES = {"Apple": "AAPL", "Tesla": "TSLA", "EUR/USD": "EURUSD=X"}
HORIZON = 5                  # jours pour juger si un signal avait raison
FICHIER_ETAT = "etat.json"
AVERTISSEMENT = "Ce ne sont pas des conseils financiers. Le passé ne garantit pas l'avenir."


# ---------- Calculs ----------
def ema_alpha(valeurs, alpha):
    res = []
    for v in valeurs:
        res.append(v if not res else alpha * v + (1 - alpha) * res[-1])
    return res


def sma(valeurs, n):
    res, somme = [], 0.0
    for i, v in enumerate(valeurs):
        somme += v
        if i >= n:
            somme -= valeurs[i - n]
        res.append(somme / n if i >= n - 1 else None)
    return res


def rsi_serie(close, n=14):
    gains = [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    pertes = [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    g, p = ema_alpha(gains, 1 / n), ema_alpha(pertes, 1 / n)
    res = [None]
    for gi, pi in zip(g, p):
        if pi == 0:
            res.append(50.0 if gi == 0 else 100.0)
        else:
            res.append(100 - 100 / (1 + gi / pi))
    return res


def calcul_signaux(close):
    rsi = rsi_serie(close)
    e12, e26 = ema_alpha(close, 2 / 13), ema_alpha(close, 2 / 27)
    macd = [a - b for a, b in zip(e12, e26)]
    macd_sig = ema_alpha(macd, 2 / 10)
    s20, s50 = sma(close, 20), sma(close, 50)

    signaux = []
    for i in range(len(close)):
        if s50[i] is None or rsi[i] is None:
            signaux.append(None)
            continue
        score = int(rsi[i] < 30) - int(rsi[i] > 70)
        score += 1 if macd[i] > macd_sig[i] else -1
        score += 1 if s20[i] > s50[i] else -1
        if score >= 2:
            signaux.append("ACHAT")
        elif score <= -2:
            signaux.append("VENTE")
        else:
            signaux.append("NEUTRE")
    return signaux, rsi


def taux_reussite(close, signaux):
    """% de fois où le prix a évolué dans le sens du signal, HORIZON jours après."""
    bons = total = 0
    for i in range(len(close) - HORIZON):
        s = signaux[i]
        if s not in ("ACHAT", "VENTE"):
            continue
        total += 1
        futur = close[i + HORIZON]
        if (s == "ACHAT" and futur > close[i]) or (s == "VENTE" and futur < close[i]):
            bons += 1
    return (100 * bons / total if total else None), total


def telecharger(symbole):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbole}"
    r = requests.get(url, params={"range": "2y", "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return [c for c in closes if c is not None]


def analyse(symbole):
    close = telecharger(symbole)
    signaux, rsi = calcul_signaux(close)
    taux, n = taux_reussite(close, signaux)
    return {"signal": signaux[-1], "prix": close[-1], "rsi": rsi[-1],
            "taux": taux, "n": n}


def calculer_tout():
    resultats = {}
    for nom, symbole in SYMBOLES.items():
        try:
            resultats[nom] = analyse(symbole)
        except Exception as e:
            print("Erreur données", nom, e)
            resultats[nom] = None
    return resultats


def texte(nom, r):
    fiabilite = (f"{r['taux']:.0f}% sur {r['n']} signaux passés (à {HORIZON} jours)"
                 if r["taux"] is not None else "pas assez de données")
    return (f"{nom} : {r['signal']}\n"
            f"Prix {r['prix']:.4f} | RSI {r['rsi']:.0f}\n"
            f"Fiabilité passée : {fiabilite}")


def rapport(resultats):
    lignes = [texte(nom, r) if r else f"{nom} : données indisponibles"
              for nom, r in resultats.items()]
    lignes.append(AVERTISSEMENT)
    return "\n\n".join(lignes)


# ---------- Mémoire entre deux exécutions ----------
def charger_etat():
    try:
        with open(FICHIER_ETAT) as f:
            etat = json.load(f)
    except Exception:
        etat = {}
    etat.setdefault("offset", None)
    etat.setdefault("abonnes", [])
    etat.setdefault("signaux", {})
    return etat


def sauver_etat(etat):
    with open(FICHIER_ETAT, "w") as f:
        json.dump(etat, f)


# ---------- Programme principal ----------
def main():
    token = os.environ["BOT_TOKEN"]
    api = f"https://api.telegram.org/bot{token}"

    def envoyer(chat_id, message):
        try:
            requests.post(api + "/sendMessage",
                          json={"chat_id": chat_id, "text": message}, timeout=20)
        except Exception as e:
            print("Envoi impossible :", e)

    etat = charger_etat()

    # 1) Messages reçus depuis la dernière exécution
    params = {"timeout": 0}
    if etat["offset"]:
        params["offset"] = etat["offset"]
    reponse = requests.get(api + "/getUpdates", params=params, timeout=30).json()
    if not reponse.get("ok"):
        print("Telegram a refusé la demande (token incorrect ?) :", reponse)
        sys.exit(1)

    # 2) Analyse du marché (une seule fois par exécution)
    resultats = calculer_tout()

    nouveaux = set()
    for u in reponse.get("result", []):
        etat["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        commande = msg.get("text", "")
        if not chat:
            continue
        if commande.startswith("/start"):
            if chat not in etat["abonnes"]:
                etat["abonnes"].append(chat)
            nouveaux.add(chat)
            envoyer(chat, "Bot actif. Tu recevras une alerte quand un signal change "
                          "(vérification toutes les ~15 min). /signaux pour voir la situation.")
            envoyer(chat, rapport(resultats))
        elif commande.startswith("/signaux"):
            envoyer(chat, rapport(resultats))

    # 3) Alertes quand un signal change
    for nom, r in resultats.items():
        if not r:
            continue
        if r["signal"] != "NEUTRE" and etat["signaux"].get(nom) != r["signal"]:
            for chat in etat["abonnes"]:
                if chat not in nouveaux:
                    envoyer(chat, texte(nom, r))
        etat["signaux"][nom] = r["signal"]

    sauver_etat(etat)
    print("Terminé.")


if __name__ == "__main__":
    main()
