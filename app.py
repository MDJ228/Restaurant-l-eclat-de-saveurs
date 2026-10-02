import json
import os
import re
import secrets
import threading
import unicodedata
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changez-cette-cle-avant-la-mise-en-ligne")

MODES = {"sur_place": "Sur place", "livraison": "Livraison"}
PAIEMENTS = {"mobile_money": "Mobile Money", "carte": "Carte bancaire"}


def lire(nom, defaut):
    try:
        with open(os.path.join(BASE, nom), "r", encoding="utf-8") as fichier:
            return json.load(fichier)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def ecrire(nom, contenu):
    with open(os.path.join(BASE, nom), "w", encoding="utf-8") as fichier:
        json.dump(contenu, fichier, ensure_ascii=False, indent=2)


EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".svg")
IMAGE_PAR_DEFAUT = "images/placeholder.svg"


def slug(texte):
    texte = texte.lower().replace("œ", "oe").replace("æ", "ae")
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", texte).strip("-")


def images_disponibles():
    dossier = os.path.join(BASE, "static", "images")
    trouvees = {}
    if os.path.isdir(dossier):
        for nom in sorted(os.listdir(dossier)):
            racine, extension = os.path.splitext(nom)
            if extension.lower() in EXTENSIONS:
                trouvees.setdefault(slug(racine), "images/" + nom)
    return trouvees


def associer_image(plat, images):
    declaree = os.path.splitext(os.path.basename(plat.get("image", "")))[0]
    cles = [declaree, f"image{plat['id']}", f"image-{plat['id']}", f"plat{plat['id']}",
            str(plat["id"]), plat["nom"]]
    for cle in cles:
        if cle and slug(cle) in images:
            return images[slug(cle)]
    return None


def image_du_site(*noms):
    images = images_disponibles()
    return next((images[n] for n in noms if n in images), None)


VERROU = threading.Lock()
DELAI_RESEAU = 6
REESSAI_ECHEC = timedelta(days=1)


def requete_json(url, entetes=None):
    en_tetes = {"User-Agent": "EclatDeSaveurs/1.0"}
    en_tetes.update(entetes or {})
    with urlopen(Request(url, headers=en_tetes), timeout=DELAI_RESEAU) as reponse:
        return json.load(reponse)


def chercher_pexels(requete):
    cle = os.environ.get("PEXELS_API_KEY")
    if not cle:
        return None
    adresse = "https://api.pexels.com/v1/search?" + urlencode(
        {"query": requete, "per_page": 1, "orientation": "landscape"})
    photos = requete_json(adresse, {"Authorization": cle}).get("photos", [])
    return photos[0]["src"]["large"] if photos else None


def chercher_mealdb(requete):
    adresse = "https://www.themealdb.com/api/json/v1/1/search.php?" + urlencode({"s": requete})
    repas = requete_json(adresse).get("meals") or []
    return repas[0]["strMealThumb"] if repas else None


def chercher_wikipedia(requete):
    adresse = "https://en.wikipedia.org/w/api.php?" + urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrsearch": requete,
        "gsrlimit": 3, "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 700})
    pages = requete_json(adresse).get("query", {}).get("pages", {}).values()
    for page in sorted(pages, key=lambda p: p.get("index", 99)):
        if "thumbnail" in page:
            return page["thumbnail"]["source"]
    return None


def image_distante(plat):
    cle = str(plat["id"])
    requete = plat.get("recherche") or plat["nom"]
    with VERROU:
        entree = lire("images_cache.json", {}).get(cle)
    if entree and entree.get("requete") == requete:
        recente = datetime.now() - datetime.fromisoformat(entree["date"]) < REESSAI_ECHEC
        if entree["url"] or recente:
            return entree["url"]

    url = None
    for chercheur in (chercher_pexels, chercher_mealdb, chercher_wikipedia):
        try:
            url = chercheur(requete)
        except Exception:
            url = None
        if url:
            break

    with VERROU:
        cache = lire("images_cache.json", {})
        cache[cle] = {"requete": requete, "url": url, "date": datetime.now().isoformat()}
        ecrire("images_cache.json", cache)
    return url


def charger():
    data = lire("data.json", {})
    images = images_disponibles()
    for plat in data.get("menu", []):
        locale = associer_image(plat, images)
        declaree = plat.get("image", "")
        if locale:
            plat["image_url"] = url_for("static", filename=locale)
        elif declaree.startswith("http"):
            plat["image_url"] = declaree
        else:
            plat["image_url"] = url_for("image_plat", plat_id=plat["id"])
    return data


def chiffres(texte):
    return re.sub(r"\D", "", texte or "")


def adresse_sure(cible, defaut="index"):
    if cible and cible.startswith("/") and not cible.startswith("//"):
        return cible
    return url_for(defaut)


def utilisateur_courant():
    email = session.get("user")
    if not email:
        return None
    return next((u for u in lire("users.json", []) if u["email"] == email), None)


def lignes_panier():
    index = {p["id"]: p for p in charger().get("menu", [])}
    lignes = []
    for cle, quantite in session.get("panier", {}).items():
        plat = index.get(int(cle))
        if plat:
            lignes.append({"plat": plat, "quantite": quantite,
                           "total": round(plat["prix"] * quantite, 2)})
    return lignes


def totaux(lignes, mode):
    sous_total = round(sum(l["total"] for l in lignes), 2)
    frais = charger().get("frais_livraison", 0) if mode == "livraison" and lignes else 0
    return sous_total, frais, round(sous_total + frais, 2)


@app.context_processor
def contexte():
    return {
        "data": charger(),
        "nb_panier": sum(session.get("panier", {}).values()),
        "utilisateur": utilisateur_courant(),
        "annee": datetime.now().year,
        "logo_url": image_du_site("logo"),
        "fond_url": image_du_site("font", "fond", "background"),
        "accueil_url": image_du_site("restaurant", "accueil", "hero", "logo"),
    }


@app.route("/")
def index():
    populaires = [p for p in charger().get("menu", []) if p.get("populaire")]
    return render_template("index.html", populaires=populaires)


@app.route("/image/<int:plat_id>")
def image_plat(plat_id):
    plat = next((p for p in lire("data.json", {}).get("menu", []) if p["id"] == plat_id), None)
    if not plat:
        abort(404)
    url = image_distante(plat)
    reponse = redirect(url or url_for("static", filename=IMAGE_PAR_DEFAUT))
    reponse.headers["Cache-Control"] = "public, max-age=86400" if url else "no-cache"
    return reponse


@app.route("/menu")
def menu():
    return render_template("menu.html")


@app.route("/panier")
def panier():
    lignes = lignes_panier()
    sous_total, _, _ = totaux(lignes, "sur_place")
    return render_template("panier.html", lignes=lignes, sous_total=sous_total)


@app.route("/panier/ajouter/<int:plat_id>", methods=["POST"])
def ajouter(plat_id):
    plat = next((p for p in charger()["menu"] if p["id"] == plat_id), None)
    if not plat:
        abort(404)
    contenu = session.get("panier", {})
    cle = str(plat_id)
    contenu[cle] = min(contenu.get(cle, 0) + 1, 20)
    session["panier"] = contenu
    flash(f"{plat['nom']} a été ajouté au panier.", "succes")
    return redirect(adresse_sure(request.form.get("next")))


@app.route("/panier/modifier/<int:plat_id>", methods=["POST"])
def modifier(plat_id):
    contenu = session.get("panier", {})
    cle = str(plat_id)
    if cle in contenu:
        contenu[cle] += 1 if request.form.get("action") == "plus" else -1
        contenu[cle] = min(contenu[cle], 20)
        if contenu[cle] <= 0:
            del contenu[cle]
        session["panier"] = contenu
    return redirect(url_for("panier"))


@app.route("/panier/supprimer/<int:plat_id>", methods=["POST"])
def supprimer(plat_id):
    contenu = session.get("panier", {})
    contenu.pop(str(plat_id), None)
    session["panier"] = contenu
    return redirect(url_for("panier"))


@app.route("/panier/vider", methods=["POST"])
def vider():
    session.pop("panier", None)
    flash("Votre panier a été vidé.", "info")
    return redirect(url_for("panier"))


def valider_commande(f):
    erreurs = []
    if not f.get("nom", "").strip():
        erreurs.append("Indiquez votre nom.")
    if len(chiffres(f.get("telephone"))) < 8:
        erreurs.append("Indiquez un numéro de téléphone valide.")
    email = f.get("email", "").strip()
    if email and not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        erreurs.append("L'adresse e-mail n'est pas valide.")
    if f.get("mode") not in MODES:
        erreurs.append("Choisissez sur place ou livraison.")
    if f.get("mode") == "livraison" and not f.get("adresse", "").strip():
        erreurs.append("Indiquez l'adresse de livraison.")
    if f.get("paiement") not in PAIEMENTS:
        erreurs.append("Choisissez un moyen de paiement.")
    if f.get("paiement") == "mobile_money":
        if f.get("operateur") not in charger().get("operateurs_mobile_money", []):
            erreurs.append("Choisissez votre opérateur Mobile Money.")
        if not 8 <= len(chiffres(f.get("numero_mm"))) <= 15:
            erreurs.append("Le numéro Mobile Money n'est pas valide.")
    if f.get("paiement") == "carte":
        erreurs += valider_carte(f)
    return erreurs


def valider_carte(f):
    erreurs = []
    if not f.get("titulaire", "").strip():
        erreurs.append("Indiquez le nom du titulaire de la carte.")
    numero = chiffres(f.get("numero_carte"))
    if not 13 <= len(numero) <= 19 or not luhn(numero):
        erreurs.append("Le numéro de carte n'est pas valide.")
    expiration = re.match(r"^(\d{2})\s*/\s*(\d{2})$", f.get("expiration", "").strip())
    if not expiration or not 1 <= int(expiration.group(1)) <= 12:
        erreurs.append("La date d'expiration doit être au format MM/AA.")
    else:
        maintenant = datetime.now()
        if (2000 + int(expiration.group(2)), int(expiration.group(1))) < (maintenant.year, maintenant.month):
            erreurs.append("Cette carte est expirée.")
    if not re.match(r"^\d{3,4}$", f.get("cvv", "").strip()):
        erreurs.append("Le code de sécurité (CVV) n'est pas valide.")
    return erreurs


def luhn(numero):
    somme = 0
    for i, c in enumerate(reversed(numero)):
        n = int(c)
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        somme += n
    return somme % 10 == 0


@app.route("/commande", methods=["GET", "POST"])
def commande():
    lignes = lignes_panier()
    if not lignes:
        flash("Ajoutez des plats à votre panier avant de commander.", "info")
        return redirect(url_for("menu"))

    u = utilisateur_courant() or {}
    valeurs = {
        "nom": u.get("nom", ""), "telephone": u.get("telephone", ""),
        "email": u.get("email", ""), "adresse": u.get("adresse", ""),
        "mode": "sur_place", "paiement": u.get("paiement_prefere", "mobile_money"),
        "operateur": u.get("operateur", ""),
        "numero_mm": u.get("numero_mm") or u.get("telephone", ""),
    }

    if request.method == "POST":
        f = request.form
        valeurs = f.to_dict()
        erreurs = valider_commande(f)
        if not erreurs:
            return enregistrer_commande(f, lignes, u)
        for erreur in erreurs:
            flash(erreur, "erreur")

    sous_total, frais, total = totaux(lignes, valeurs.get("mode"))
    return render_template("commande.html", lignes=lignes, valeurs=valeurs,
                           sous_total=sous_total, frais=frais, total=total)


def enregistrer_commande(f, lignes, u):
    devise = charger().get("devise", "€")
    mode = f["mode"]
    sous_total, frais, total = totaux(lignes, mode)

    if f["paiement"] == "mobile_money":
        numero = chiffres(f["numero_mm"])
        detail = f"{f['operateur']} - numéro se terminant par {numero[-3:]}"
    else:
        detail = f"Carte se terminant par {chiffres(f['numero_carte'])[-4:]}"

    ref = f"CMD-{datetime.now():%y%m%d}-{secrets.token_hex(2).upper()}"
    nouvelle = {
        "ref": ref,
        "date": f"{datetime.now():%d/%m/%Y à %H:%M}",
        "compte": u.get("email"),
        "client": {"nom": f["nom"].strip(), "telephone": f["telephone"].strip(),
                   "email": f.get("email", "").strip()},
        "mode": MODES[mode],
        "adresse": f.get("adresse", "").strip() if mode == "livraison" else "",
        "table": f.get("table", "").strip() if mode == "sur_place" else "",
        "paiement": {"moyen": PAIEMENTS[f["paiement"]], "detail": detail},
        "lignes": [{"nom": l["plat"]["nom"], "prix": l["plat"]["prix"],
                    "quantite": l["quantite"], "total": l["total"]} for l in lignes],
        "sous_total": sous_total, "frais": frais, "total": total, "devise": devise,
    }
    commandes = lire("commandes.json", [])
    commandes.append(nouvelle)
    ecrire("commandes.json", commandes)

    session["commandes"] = session.get("commandes", []) + [ref]
    session.pop("panier", None)
    flash("Paiement accepté. Votre commande est confirmée.", "succes")
    return redirect(url_for("recu", ref=ref))


def commande_autorisee(ref):
    commande_trouvee = next((c for c in lire("commandes.json", []) if c["ref"] == ref), None)
    if not commande_trouvee:
        abort(404)
    u = utilisateur_courant()
    if ref in session.get("commandes", []) or (u and commande_trouvee.get("compte") == u["email"]):
        return commande_trouvee
    abort(403)


@app.route("/commande/<ref>")
def recu(ref):
    return render_template("recu.html", c=commande_autorisee(ref))


@app.route("/commande/<ref>/recu.pdf")
def recu_pdf(ref):
    c = commande_autorisee(ref)
    try:
        contenu = generer_pdf(c)
    except ImportError:
        flash("Le téléchargement PDF nécessite le module reportlab. Utilisez Imprimer, puis Enregistrer en PDF.", "erreur")
        return redirect(url_for("recu", ref=ref))
    return send_file(contenu, mimetype="application/pdf", as_attachment=True,
                     download_name=f"recu-{ref}.pdf")


def generer_pdf(c):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    data = charger()
    contact = data.get("contact", {})
    devise = c["devise"]
    bordeaux = colors.HexColor("#7d1d28")
    styles = getSampleStyleSheet()
    titre = ParagraphStyle("titre", parent=styles["Title"], textColor=bordeaux, fontSize=22)
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontSize=10, leading=14)
    centre = ParagraphStyle("centre", parent=normal, alignment=1, textColor=colors.grey)

    def p(texte, style=normal):
        return Paragraph(escape(str(texte)), style)

    def prix(valeur):
        return f"{valeur:.2f} {devise}"

    tampon = BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=f"Reçu {c['ref']}")
    elements = [
        p(data.get("restaurant", ""), titre),
        p(f"{contact.get('adresse', '')} - {contact.get('telephone', '')}", centre),
        Spacer(1, 10 * mm),
        p(f"Reçu de commande {c['ref']}", styles["Heading2"]),
        p(f"Date : {c['date']}"),
        p(f"Client : {c['client']['nom']} - {c['client']['telephone']}"),
        p(f"Mode : {c['mode']}"),
    ]
    if c["adresse"]:
        elements.append(p(f"Adresse de livraison : {c['adresse']}"))
    if c["table"]:
        elements.append(p(f"Table : {c['table']}"))
    elements += [p(f"Paiement : {c['paiement']['moyen']} ({c['paiement']['detail']})"),
                 Spacer(1, 8 * mm)]

    lignes = [["Plat", "Prix", "Qté", "Total"]]
    lignes += [[p(l["nom"]), prix(l["prix"]), l["quantite"], prix(l["total"])] for l in c["lignes"]]
    lignes.append(["", "", "Sous-total", prix(c["sous_total"])])
    if c["frais"]:
        lignes.append(["", "", "Livraison", prix(c["frais"])])
    lignes.append(["", "", "Total payé", prix(c["total"])])
    tableau = Table(lignes, colWidths=[78 * mm, 30 * mm, 28 * mm, 34 * mm])
    tableau.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), bordeaux),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, len(c["lignes"])), 0.4, colors.lightgrey),
        ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements += [tableau, Spacer(1, 12 * mm), p("Merci pour votre confiance et bon appétit !", centre)]
    doc.build(elements)
    tampon.seek(0)
    return tampon


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    cible = request.values.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        utilisateur = next((u for u in lire("users.json", []) if u["email"] == email), None)
        if utilisateur and check_password_hash(utilisateur["mot_de_passe"], request.form.get("mot_de_passe", "")):
            session["user"] = email
            flash(f"Bienvenue {utilisateur['nom']} !", "succes")
            return redirect(adresse_sure(cible))
        flash("E-mail ou mot de passe incorrect.", "erreur")
    return render_template("connexion.html", cible=cible)


CHAMPS_PROFIL = ["nom", "telephone", "adresse", "paiement_prefere", "operateur", "numero_mm"]


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    cible = request.values.get("next", "")
    valeurs = {}
    if request.method == "POST":
        f = request.form
        valeurs = f.to_dict()
        email = f.get("email", "").strip().lower()
        utilisateurs = lire("users.json", [])
        erreurs = []
        if not f.get("nom", "").strip():
            erreurs.append("Indiquez votre nom.")
        if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            erreurs.append("L'adresse e-mail n'est pas valide.")
        if any(u["email"] == email for u in utilisateurs):
            erreurs.append("Un compte existe déjà avec cet e-mail.")
        if len(f.get("mot_de_passe", "")) < 6:
            erreurs.append("Le mot de passe doit contenir au moins 6 caractères.")
        if f.get("mot_de_passe") != f.get("confirmation"):
            erreurs.append("Les deux mots de passe ne correspondent pas.")
        if erreurs:
            for erreur in erreurs:
                flash(erreur, "erreur")
        else:
            nouveau = {c: f.get(c, "").strip() for c in CHAMPS_PROFIL}
            nouveau["email"] = email
            nouveau["mot_de_passe"] = generate_password_hash(f["mot_de_passe"])
            utilisateurs.append(nouveau)
            ecrire("users.json", utilisateurs)
            session["user"] = email
            flash("Votre compte a bien été créé.", "succes")
            return redirect(adresse_sure(cible))
    return render_template("inscription.html", cible=cible, valeurs=valeurs)


@app.route("/deconnexion")
def deconnexion():
    session.pop("user", None)
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("index"))


@app.route("/compte", methods=["GET", "POST"])
def compte():
    u = utilisateur_courant()
    if not u:
        return redirect(url_for("connexion", next=url_for("compte")))
    if request.method == "POST":
        utilisateurs = lire("users.json", [])
        for utilisateur in utilisateurs:
            if utilisateur["email"] == u["email"]:
                for champ in CHAMPS_PROFIL:
                    utilisateur[champ] = request.form.get(champ, "").strip()
        ecrire("users.json", utilisateurs)
        flash("Vos informations ont été enregistrées.", "succes")
        return redirect(url_for("compte"))
    commandes = [c for c in lire("commandes.json", []) if c.get("compte") == u["email"]]
    return render_template("compte.html", valeurs=u, commandes=list(reversed(commandes)))


@app.route("/contact", methods=["GET", "POST"])
def contact():
    adresse = charger().get("contact", {}).get("adresse", "")
    carte = "https://www.google.com/maps/search/?api=1&query=" + quote(adresse)
    valeurs = {}
    if request.method == "POST":
        f = request.form
        valeurs = f.to_dict()
        if not (f.get("nom", "").strip() and f.get("message", "").strip()
                and re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", f.get("email", "").strip())):
            flash("Renseignez votre nom, une adresse e-mail valide et votre message.", "erreur")
        else:
            messages = lire("messages.json", [])
            messages.append({"date": f"{datetime.now():%d/%m/%Y %H:%M}",
                             "nom": f["nom"].strip(), "email": f["email"].strip(),
                             "sujet": f.get("sujet", "").strip(), "message": f["message"].strip()})
            ecrire("messages.json", messages)
            flash("Merci, votre message a bien été envoyé. Nous vous répondons rapidement.", "succes")
            return redirect(url_for("contact"))
    return render_template("contact.html", valeurs=valeurs, carte=carte)


if __name__ == "__main__":
    app.run(debug=True)
