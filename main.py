from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from datetime import datetime, timedelta
import uuid
import os
import logging
import boto3
from botocore.client import Config

R2_ENDPOINT = "https://b16fe0cbd4f60d167a90c1c73c4f9697.r2.cloudflarestorage.com"
R2_BUCKET = "newtoncheat"

AWS_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("R2_SECRET_KEY")

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)


# -------------------- Config --------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "devkey")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///licences.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "changeme")

# -------------------- Database --------------------

db = SQLAlchemy(app)

class Licence(db.Model):
    licence_code = db.Column(db.String(20), primary_key=True)
    hwid = db.Column(db.String(100))
    expiry = db.Column(db.DateTime)
    activated = db.Column(db.Boolean, default=False)

with app.app_context():
    db.create_all()

# -------------------- Rate Limiting --------------------

limiter = Limiter(get_remote_address, app=app)

# -------------------- Admin Check --------------------

def require_admin():
    api_key = request.headers.get("X-API-KEY")
    return api_key == ADMIN_API_KEY

# -------------------- Routes --------------------

@app.route("/generate_code", methods=["POST"])
def generate_code():
    if not require_admin():
        return jsonify({"msg": "Unauthorized"}), 401

    licence_code = str(uuid.uuid4()).split("-")[0].upper()
    expiry = datetime.utcnow() + timedelta(days=30)

    new_licence = Licence(licence_code=licence_code, expiry=expiry)
    db.session.add(new_licence)
    db.session.commit()

    return jsonify({"licence_code": licence_code, "expiry": expiry.isoformat()})


@app.route("/activate", methods=["POST"])
@limiter.limit("10 per minute")
def activate():
    data = request.get_json() or {}
    code = data.get("licence_code")
    hwid = data.get("hwid")

    if not code or not hwid:
        return jsonify({"msg": "Missing licence_code or hwid"}), 400

    licence = Licence.query.get(code)
    if not licence:
        return jsonify({"msg": "Invalid licence code"}), 404

    if licence.activated and licence.hwid != hwid:
        return jsonify({"msg": "Licence already activated on a different machine."}), 403

    licence.hwid = hwid
    licence.activated = True
    db.session.commit()

    return jsonify({"msg": "Licence activated successfully.", "licence_code": code})


@app.route("/check", methods=["POST"])
@limiter.limit("30 per minute")
def check():
    data = request.get_json() or {}
    code = data.get("licence_code")
    hwid = data.get("hwid")

    licence = Licence.query.get(code)
    if not licence:
        return jsonify({"msg": "Licence not found"}), 404

    if not licence.activated or licence.hwid != hwid:
        return jsonify({"msg": "HWID mismatch or licence not activated"}), 403

    if datetime.utcnow() > licence.expiry:
        return jsonify({"msg": "Licence expired."}), 403

    return jsonify({"msg": "Licence valid"}), 200


@app.route("/check_hwid", methods=["POST"])
@limiter.limit("30 per minute")
def check_hwid():
    data = request.get_json() or {}
    hwid = data.get("hwid")

    if not hwid:
        return jsonify({"msg": "Missing HWID"}), 400

    licence = Licence.query.filter_by(hwid=hwid).first()
    if not licence:
        return jsonify({"msg": "HWID not activated."}), 404

    if datetime.utcnow() > licence.expiry:
        return jsonify({"msg": "Licence expired."}), 403

    return jsonify({"msg": "Licence valid"}), 200


@app.route("/admin/list_licences", methods=["GET"])
def list_licences():
    if not require_admin():
        return jsonify({"msg": "Unauthorized"}), 401

    licences = Licence.query.all()

    return jsonify([
        {
            "licence_code": l.licence_code,
            "hwid": l.hwid,
            "expiry": l.expiry.isoformat() if l.expiry else None,
            "activated": l.activated
        } for l in licences
    ])


@app.route("/get_link/<path:filename>", methods=["GET"])
@limiter.limit("30 per minute")
def get_link(filename):
    # ✅ Licence check
    code = request.args.get("licence_code")
    hwid = request.args.get("hwid")

    licence = Licence.query.get(code)
    if not licence:
        return jsonify({"msg": "Licence not found"}), 404

    if not licence.activated or licence.hwid != hwid:
        return jsonify({"msg": "HWID mismatch or licence not activated"}), 403

    if datetime.utcnow() > licence.expiry:
        return jsonify({"msg": "Licence expired."}), 403

    # ✅ Generate the signed R2 link
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': R2_BUCKET, 'Key': filename},
            ExpiresIn=120
        )
    except Exception as e:
        return jsonify({"msg": f"Error generating download link: {str(e)}"}), 500

    return jsonify({"url": url})


# -------------------- Run Server --------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
