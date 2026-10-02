document.addEventListener("DOMContentLoaded", () => {
    const burger = document.getElementById("burger");
    const menuNav = document.getElementById("menu-nav");
    if (burger && menuNav) {
        burger.addEventListener("click", () => menuNav.classList.toggle("ouvert"));
    }

    document.querySelectorAll(".flash").forEach((message) => {
        setTimeout(() => message.classList.add("cache"), 5000);
    });

    const filtres = document.querySelectorAll(".filtre");
    const blocs = document.querySelectorAll(".categorie-bloc");
    filtres.forEach((bouton) => {
        bouton.addEventListener("click", () => {
            filtres.forEach((f) => f.classList.toggle("actif", f === bouton));
            blocs.forEach((bloc) => {
                bloc.hidden = bouton.dataset.cat !== "tout" && bloc.dataset.cat !== bouton.dataset.cat;
            });
        });
    });

    const formulaire = document.getElementById("form-commande");
    if (!formulaire) return;

    const sousTotal = parseFloat(formulaire.dataset.sousTotal);
    const frais = parseFloat(formulaire.dataset.frais);
    const devise = formulaire.dataset.devise;

    const basculer = (id, actif) => {
        const bloc = document.getElementById(id);
        bloc.hidden = !actif;
        bloc.querySelectorAll("input, select").forEach((champ) => {
            champ.disabled = !actif;
        });
    };

    const mettreAJour = () => {
        const mode = formulaire.querySelector('input[name="mode"]:checked').value;
        const paiement = formulaire.querySelector('input[name="paiement"]:checked').value;
        basculer("bloc-sur_place", mode === "sur_place");
        basculer("bloc-livraison", mode === "livraison");
        basculer("bloc-mobile_money", paiement === "mobile_money");
        basculer("bloc-carte", paiement === "carte");
        document.getElementById("ligne-frais").hidden = mode !== "livraison";
        const total = (sousTotal + (mode === "livraison" ? frais : 0)).toFixed(2) + " " + devise;
        document.getElementById("total-resume").textContent = total;
        document.getElementById("total-bouton").textContent = total;
    };

    formulaire.querySelectorAll('input[name="mode"], input[name="paiement"]').forEach((radio) => {
        radio.addEventListener("change", mettreAJour);
    });

    const numeroCarte = formulaire.querySelector('input[name="numero_carte"]');
    numeroCarte.addEventListener("input", () => {
        const chiffres = numeroCarte.value.replace(/\D/g, "").slice(0, 19);
        numeroCarte.value = chiffres.replace(/(\d{4})(?=\d)/g, "$1 ");
    });

    const expiration = formulaire.querySelector('input[name="expiration"]');
    expiration.addEventListener("input", () => {
        const chiffres = expiration.value.replace(/\D/g, "").slice(0, 4);
        expiration.value = chiffres.length > 2 ? chiffres.slice(0, 2) + "/" + chiffres.slice(2) : chiffres;
    });

    mettreAJour();
});
