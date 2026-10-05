# -*- coding: utf-8 -*-

import csv
import statistics

from qgis.PyQt.QtWidgets import QFileDialog, QInputDialog, QMessageBox
from qgis.PyQt.QtCore import QDir
from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsField, QgsFields,
    QgsVectorFileWriter, QgsPointXY, QgsWkbTypes
)
from PyQt5.QtCore import QVariant
import processing


def ask_file(title):
    path, _ = QFileDialog.getOpenFileName(None, title, QDir.homePath(), 'Shapefile (*.shp)')
    return path


def ask_folder(title):
    path = QFileDialog.getExistingDirectory(None, title, QDir.homePath())
    return path


def ask_number(label, default=0.0, minv=-1_000_000_000.0, maxv=1_000_000_000.0, decimals=3):
    val, ok = QInputDialog.getDouble(None, "CDA-Mini - Parametres", label, default, minv, maxv, decimals)
    if not ok:
        raise SystemExit("Annule par l'utilisateur.")
    return val


def fail(msg):
    QMessageBox.critical(None, "CDA-Mini - Erreur", msg)
    raise SystemExit(msg)


shp1_path = ask_file("Selectionner le trait de cote DATE 1 (le plus ANCIEN)")
if not shp1_path:
    raise SystemExit("Selection annulee.")

shp2_path = ask_file("Selectionner le trait de cote DATE 2 (le plus RECENT)")
if not shp2_path:
    raise SystemExit("Selection annulee.")

out_folder = ask_folder("Dossier de sortie pour les resultats")
if not out_folder:
    raise SystemExit("Selection annulee.")
out_folder = out_folder.rstrip('/\\') + '/'

spacing = ask_number("Espacement entre transects (m) :", default=20, minv=0.01, decimals=2)
transect_len = ask_number("Longueur des transects, de chaque cote de la baseline (m) :", default=200, minv=0.01, decimals=2)
years = ask_number("Intervalle de temps entre les 2 dates (annees) :", default=1, minv=0.0001, decimals=4)
offset_dist = ask_number(
    "Distance de decalage pour generer la baseline (m).\n"
    "Positif = baseline cote large/mer, Negatif = baseline cote terre.\n"
    "Doit etre superieure a la plus grande variation attendue entre les 2 traits :",
    default=100, minv=-1_000_000, decimals=2
)
min_segment_len = max(2.0 * spacing, 10.0)  # segments plus courts que ca => pas de transect possible

def load_and_fix(path, name):
    lyr = QgsVectorLayer(path, name, 'ogr')
    if not lyr.isValid():
        fail(f"Impossible de charger le fichier :\n{path}")
    if lyr.crs().isGeographic():
        QMessageBox.warning(
            None, "CDA-Mini - Attention",
            f"La couche '{name}' est en coordonnees GEOGRAPHIQUES (degres).\n"
            "Les distances calculees seront fausses. Reprojetez vos shapefiles "
            "dans un SCR projete (ex: UTM) avant de continuer."
        )
    fixed = processing.run("native:fixgeometries", {'INPUT': lyr, 'METHOD': 1, 'OUTPUT': 'memory:'})['OUTPUT']
    if fixed.featureCount() == 0:
        fail(f"La couche '{name}' est vide apres correction des geometries.")
    return fixed


def get_all_parts(layer, name):
    """Fusionne ce qui peut l'etre (segments qui se touchent bout a bout),
    puis retourne la liste de TOUTES les parties (QgsGeometry simple-partie),
    sans en jeter aucune. Retourne aussi la geometrie complete (multi-partie)
    pour servir de cible d'intersection."""
    merged = processing.run("native:mergelines", {'INPUT': layer, 'OUTPUT': 'memory:'})['OUTPUT']
    feats = list(merged.getFeatures())
    if not feats:
        fail(f"Geometrie vide pour '{name}' apres fusion.")

    full_geom = QgsGeometry.unaryUnion([f.geometry() for f in feats])

    if full_geom.isMultipart():
        parts = list(full_geom.asGeometryCollection())
    else:
        parts = [full_geom]

    # Filtrer les parties degenerees (trop courtes pour porter un transect)
    kept, discarded = [], []
    for p in parts:
        if p.length() >= min_segment_len:
            kept.append(p)
        else:
            discarded.append(p)

    total_len = sum(p.length() for p in parts)
    kept_len = sum(p.length() for p in kept)

    msg_lines = [f"'{name}' contient {len(parts)} segment(s), longueur totale {total_len:.1f} m."]
    if discarded:
        msg_lines.append(
            f"{len(discarded)} segment(s) trop court(s) (< {min_segment_len:.1f} m, "
            f"soit {total_len - kept_len:.1f} m au total) ont ete ignores car trop "
            f"petits pour porter un transect."
        )
    msg_lines.append(f"{len(kept)} segment(s) traites, {kept_len:.1f} m au total.")
    QMessageBox.information(None, "CDA-Mini - Information", "\n".join(msg_lines))

    return kept, full_geom


shore1_lyr = load_and_fix(shp1_path, 'shoreline_date1')
shore2_lyr = load_and_fix(shp2_path, 'shoreline_date2')

shore1_parts, geom1_full = get_all_parts(shore1_lyr, 'trait de cote DATE 1')
_, geom2_full = get_all_parts(shore2_lyr, 'trait de cote DATE 2')

crs_authid = shore1_lyr.crs().authid()

if not shore1_parts:
    fail("Aucun segment du trait de cote DATE 1 n'est assez long pour generer des transects.")



all_baseline_geoms = []      # pour export/inspection
all_transect_feats = []      # liste de (geometry, seg_id)
skipped_segments = []

for seg_id, part_geom in enumerate(shore1_parts):

    seg_lyr = QgsVectorLayer('LineString?crs=' + crs_authid, f'shore1_seg_{seg_id}', 'memory')
    pr = seg_lyr.dataProvider()
    f = QgsFeature()
    f.setGeometry(part_geom)
    pr.addFeature(f)
    seg_lyr.updateExtents()

    try:
        offset_result = processing.run("native:offsetline", {
            'INPUT': seg_lyr,
            'DISTANCE': offset_dist,
            'SEGMENTS': 8,
            'JOIN_STYLE': 0,
            'MITER_LIMIT': 2,
            'OUTPUT': 'memory:'
        })['OUTPUT']
    except Exception as e:
        skipped_segments.append((seg_id, f"echec offset ({e})"))
        continue

    offset_feats = list(offset_result.getFeatures())
    if not offset_feats:
        skipped_segments.append((seg_id, "offset vide"))
        continue

    # Garder TOUTES les parties de l'offset pour ce segment (rarement multi-partie,
    # mais on ne prend pas le risque d'en perdre)
    offset_geom = QgsGeometry.unaryUnion([f.geometry() for f in offset_feats])
    offset_parts = offset_geom.asGeometryCollection() if offset_geom.isMultipart() else [offset_geom]

    for op in offset_parts:
        if op.length() < min_segment_len:
            continue
        all_baseline_geoms.append(op)

        base_lyr = QgsVectorLayer('LineString?crs=' + crs_authid, f'baseline_seg_{seg_id}', 'memory')
        pr = base_lyr.dataProvider()
        f = QgsFeature()
        f.setGeometry(op)
        pr.addFeature(f)
        base_lyr.updateExtents()

        try:
            densified = processing.run("native:densifygeometriesgivenaninterval", {
                'INPUT': base_lyr,
                'INTERVAL': spacing,
                'OUTPUT': 'memory:'
            })['OUTPUT']

            seg_transects = processing.run("native:transect", {
                'INPUT': densified,
                'LENGTH': transect_len,
                'ANGLE': 90,
                'SIDE': 2,  # les deux cotes
                'OUTPUT': 'memory:'
            })['OUTPUT']
        except Exception as e:
            skipped_segments.append((seg_id, f"echec transect ({e})"))
            continue

        for tf in seg_transects.getFeatures():
            all_transect_feats.append((tf.geometry(), seg_id))

if not all_transect_feats:
    fail("Aucun transect n'a pu etre genere sur aucun segment. Verifiez "
         "l'espacement, la longueur des transects et la distance de decalage.")

if skipped_segments:
    detail = "\n".join(f"- segment {sid} : {reason}" for sid, reason in skipped_segments)
    QMessageBox.warning(
        None, "CDA-Mini - Attention",
        f"{len(skipped_segments)} segment(s) n'ont pas pu etre traites et ont ete "
        f"ignores :\n{detail}"
    )

# Sauvegarde de toutes les baselines generees (verification visuelle possible)
baseline_all_lyr = QgsVectorLayer('LineString?crs=' + crs_authid, 'baseline_auto', 'memory')
pr = baseline_all_lyr.dataProvider()
feats = []
for g in all_baseline_geoms:
    f = QgsFeature()
    f.setGeometry(g)
    feats.append(f)
pr.addFeatures(feats)
baseline_all_lyr.updateExtents()
QgsVectorFileWriter.writeAsVectorFormat(
    baseline_all_lyr, out_folder + 'baseline_auto.shp', 'UTF-8', baseline_all_lyr.crs(), 'ESRI Shapefile'
)


def closest_intersection_point(transect_geom, shoreline_geom, ref_point):
    inter = transect_geom.intersection(shoreline_geom)
    if inter is None or inter.isEmpty():
        return None
    pts = []
    if inter.type() == QgsWkbTypes.PointGeometry:
        if inter.isMultipart():
            pts = [QgsPointXY(p) for p in inter.asMultiPoint()]
        else:
            pts = [inter.asPoint()]
    else:
        pts = [QgsPointXY(v) for v in inter.vertices()]
    if not pts:
        return None
    return min(pts, key=lambda p: ref_point.distance(p))


results = []
skipped_no_intersection = 0

for tr_id, (tgeom, seg_id) in enumerate(all_transect_feats):
    verts = list(tgeom.vertices())
    if len(verts) < 2:
        skipped_no_intersection += 1
        continue
    origin = QgsPointXY(verts[0])

    p1 = closest_intersection_point(tgeom, geom1_full, origin)
    p2 = closest_intersection_point(tgeom, geom2_full, origin)

    if p1 is None or p2 is None:
        skipped_no_intersection += 1
        continue

    dist1 = origin.distance(p1)
    dist2 = origin.distance(p2)
    nsm = dist2 - dist1
    epr = nsm / years

    results.append({
        'TR_ID': tr_id,
        'SEG_ID': seg_id,
        'DIST_D1': round(dist1, 3),
        'DIST_D2': round(dist2, 3),
        'NSM': round(nsm, 3),
        'EPR': round(epr, 4),
        'geom': tgeom,
    })

if not results:
    fail("Aucun transect n'a intersecte les 2 traits de cote. Augmentez la "
         "longueur des transects ou verifiez la distance de decalage de la "
         "baseline (offset_dist).")


out_fields = QgsFields()
out_fields.append(QgsField('TR_ID', QVariant.Int))
out_fields.append(QgsField('SEG_ID', QVariant.Int))
out_fields.append(QgsField('DIST_D1', QVariant.Double))
out_fields.append(QgsField('DIST_D2', QVariant.Double))
out_fields.append(QgsField('NSM', QVariant.Double))
out_fields.append(QgsField('EPR', QVariant.Double))

out_lyr = QgsVectorLayer('LineString?crs=' + crs_authid, 'transects_evolution', 'memory')
pr = out_lyr.dataProvider()
pr.addAttributes(out_fields)
out_lyr.updateFields()

out_feats = []
for r in results:
    f = QgsFeature(out_fields)
    f.setGeometry(r['geom'])
    f.setAttribute('TR_ID', r['TR_ID'])
    f.setAttribute('SEG_ID', r['SEG_ID'])
    f.setAttribute('DIST_D1', r['DIST_D1'])
    f.setAttribute('DIST_D2', r['DIST_D2'])
    f.setAttribute('NSM', r['NSM'])
    f.setAttribute('EPR', r['EPR'])
    out_feats.append(f)

pr.addFeatures(out_feats)
out_lyr.updateExtents()

QgsVectorFileWriter.writeAsVectorFormat(
    out_lyr, out_folder + 'transects_evolution.shp', 'UTF-8', out_lyr.crs(), 'ESRI Shapefile'
)


csv_path = out_folder + 'evolution_trait_de_cote.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['TR_ID', 'SEG_ID', 'DIST_D1', 'DIST_D2', 'NSM', 'EPR'])
    writer.writeheader()
    for r in sorted(results, key=lambda x: x['TR_ID']):
        writer.writerow({k: r[k] for k in ['TR_ID', 'SEG_ID', 'DIST_D1', 'DIST_D2', 'NSM', 'EPR']})

nsm_values = [r['NSM'] for r in results]
epr_values = [r['EPR'] for r in results]

mean_nsm = statistics.mean(nsm_values)
mean_epr = statistics.mean(epr_values)
sce = max(nsm_values) - min(nsm_values)
std_nsm = statistics.pstdev(nsm_values) if len(nsm_values) > 1 else 0.0

summary_path = out_folder + 'resume_evolution.txt'
with open(summary_path, 'w') as f:
    f.write("CDA-Mini v2 - Resume de l'evolution du trait de cote\n")
    f.write("=" * 55 + "\n")
    f.write(f"Trait de cote DATE 1 : {shp1_path}\n")
    f.write(f"Trait de cote DATE 2 : {shp2_path}\n")
    f.write(f"Intervalle de temps  : {years} an(s)\n")
    f.write(f"Espacement transects : {spacing} m\n")
    f.write(f"Nombre de segments traites (DATE 1) : {len(shore1_parts)}\n")
    f.write(f"Nombre de transects valides : {len(results)} "
            f"(ignores faute d'intersection : {skipped_no_intersection})\n\n")
    f.write(f"M-NSM (deplacement net moyen)   : {mean_nsm:.3f} m\n")
    f.write(f"M-EPR (taux annuel moyen)       : {mean_epr:.4f} m/an\n")
    f.write(f"SCE   (amplitude totale NSM)    : {sce:.3f} m\n")
    f.write(f"Ecart-type du NSM               : {std_nsm:.3f} m\n\n")
    f.write("Convention de signe : NSM/EPR positif = avancee vers le large "
            "(accretion) ; negatif = recul (erosion) ; a verifier selon le "
            "sens de decalage (offset_dist) choisi pour la baseline.\n")
    f.write("\nDetail par segment disponible dans le champ SEG_ID du shapefile "
            "transects_evolution.shp et du CSV.\n")

QMessageBox.information(
    None, "CDA-Mini - Termine",
    f"Analyse terminee : {len(results)} transects valides sur "
    f"{len(shore1_parts)} segment(s) de trait de cote.\n\n"
    f"M-NSM moyen : {mean_nsm:.2f} m\n"
    f"M-EPR moyen : {mean_epr:.3f} m/an\n"
    f"SCE : {sce:.2f} m\n\n"
    f"Fichiers crees dans :\n{out_folder}\n"
    f"- baseline_auto.shp\n"
    f"- transects_evolution.shp (champ SEG_ID = segment d'origine)\n"
    f"- evolution_trait_de_cote.csv\n"
    f"- resume_evolution.txt"
)

print("Termine. Voir dossier :", out_folder)
