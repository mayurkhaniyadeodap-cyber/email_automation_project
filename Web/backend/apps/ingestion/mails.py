"""
Outbound customer mails M1–M7 (DeoDap Care — Final Mail Flow v2.0, §7).

One registry, three language variants (English master + Hindi + Gujarati), picked
automatically from the conversation's detected language. Every send point in the
Mail Engine renders its mail through `render()` so the wording stays consistent and
matches the spec.

    M1  no identifying details found            (identity request — self-lookup miss)
    M2  video required                           (Defective / Missing / Wrong Item)
    M2P photo required, video optional           (Damaged / quality)
    M4  auto-answer & close                      (Route A — answered from APIs/playbook)
    (M3 "order# + phone required" REMOVED — order id / phone no longer block a ticket)
    M5  new ticket created                       (TKT id + tracking link)
    M6  ticket already open                      (existing TKT id + tracking link)
    M7R reminder on a waiting state              (24h)
    M7C auto-close on a waiting state            (72h; reply within 7 days reopens)

`render(mail_id, lang, **vars)` returns `(subject, body)`. Unknown language falls
back to English; missing template vars render as an empty string (never KeyError).
"""

from collections import defaultdict

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "hi", "gu")

SIGN = {
    "en": "Regards,\nDeoDap Support Team",
    "hi": "सादर,\nDeoDap सहायता टीम",
    "gu": "આભાર,\nDeoDap સહાય ટીમ",
}

# --------------------------------------------------------------------------- #
# Dynamic customer-email SUBJECT from the ticket's category + sub-topic, so a reply reflects the
# customer's actual concern (e.g. "DeoDap Support | Damaged Product - Evidence Required") instead
# of a generic "Re: <their subject>". Unknown -> "DeoDap Support | Support Request".
# --------------------------------------------------------------------------- #
SUBJECT_PREFIX = "DeoDap Support | "
DEFAULT_SUBJECT = SUBJECT_PREFIX + "Support Request"

# Ordered, MOST-SPECIFIC first. Keywords are matched against (sub_topic + category), lower-cased,
# so the delivered-item sub-types win over the broader category they live in.
_SUBJECT_RULES = (
    (("damaged", "damage", "broken", "cracked", "torn", "leaking", "dented", "shattered"),
     "Damaged Product - Evidence Required"),
    (("defective", "not working", "doesn't work", "does not work", "malfunction", "faulty",
      "dead product", "stopped working"), "Defective Product - Evidence Required"),
    (("wrong parcel", "wrong package", "wrong shipment"),
     "Wrong Parcel Received - Evidence Required"),
    (("wrong item", "wrong product", "different product", "different item", "incorrect item",
      "received wrong"), "Wrong Item Received - Evidence Required"),
    (("missing",), "Missing Item - Evidence Required"),
    (("payment", "deducted", "overcharge", "double charge", "not credited"),
     "Payment Issue - Support Request"),
    (("refund",), "Refund Status Update"),
    (("cancel",), "Order Cancellation - Support Request"),
    (("shipment", "tracking", "track", "where is my order", "delivery status", "delayed",
      "out for delivery", "rto", "return to origin"), "Shipment Tracking Information"),
)


def subject_for(category="", sub_topic=""):
    """Return the concern-based customer-email subject for a ticket's category + sub-topic.

    Falls back to "DeoDap Support | Support Request" when the concern can't be identified. Pure
    and side-effect-free -- safe to call from any outbound-email flow."""
    text = " ".join(t for t in (sub_topic or "", category or "") if t).lower()
    if text.strip():
        for keywords, tail in _SUBJECT_RULES:
            if any(k in text for k in keywords):
                return SUBJECT_PREFIX + tail
    return DEFAULT_SUBJECT

# --------------------------------------------------------------------------- #
# Registry: MAILS[mail_id][lang] = (subject, body_without_signature)
# Placeholders: {complaint_ref} {order_ref} {ticket_number} {tracking_url} {missing} {answer}
# --------------------------------------------------------------------------- #
MAILS = {
    # COD_INFO — Cash on Delivery inquiry: DeoDap is online-prepaid ONLY. Fixed auto-reply,
    # no ticket, no pincode. Signature (Regards,\nDeoDap Support Team) is appended by render().
    "COD_INFO": {
        "en": ("Cash on Delivery (COD) Information",
               "Dear Customer,\n\n"
               "Thank you for contacting DeoDap.\n\n"
               "Currently, Cash on Delivery (COD) is not available.\n\n"
               "All orders can be placed using our secure online prepaid payment methods.\n\n"
               "If you need any other assistance, simply reply to this email."),
        "hi": ("कैश ऑन डिलीवरी (COD) जानकारी",
               "प्रिय ग्राहक,\n\n"
               "DeoDap से संपर्क करने के लिए धन्यवाद।\n\n"
               "फ़िलहाल, कैश ऑन डिलीवरी (COD) उपलब्ध नहीं है।\n\n"
               "सभी ऑर्डर हमारे सुरक्षित ऑनलाइन प्रीपेड भुगतान तरीकों से किए जा सकते हैं।\n\n"
               "किसी अन्य सहायता के लिए, बस इस ईमेल का उत्तर दें।"),
        "gu": ("કેશ ઓન ડિલિવરી (COD) માહિતી",
               "પ્રિય ગ્રાહક,\n\n"
               "DeoDap નો સંપર્ક કરવા બદલ આભાર.\n\n"
               "હાલમાં, કેશ ઓન ડિલિવરી (COD) ઉપલબ્ધ નથી.\n\n"
               "બધા ઓર્ડર અમારી સુરક્ષિત ઓનલાઇન પ્રીપેડ ચુકવણી પદ્ધતિઓથી કરી શકાય છે.\n\n"
               "કોઈપણ અન્ય સહાય માટે, ફક્ત આ ઈમેલનો જવાબ આપો."),
    },
    # M1 — nothing identifying could be extracted or matched via the APIs.
    "M1": {
        "en": ("We need a few details to find your order",
               "Thank you for writing to DeoDap. We could not locate your order from "
               "this mail.\n\nPlease reply with any one of these: registered order "
               "number, email ID, mobile number, AWB / tracking number — or any other "
               "detail registered with the order."),
        "hi": ("आपका ऑर्डर ढूँढने के लिए कुछ जानकारी चाहिए",
               "DeoDap को लिखने के लिए धन्यवाद। हमें इस मेल से आपका ऑर्डर नहीं मिल सका।\n\n"
               "कृपया इनमें से कोई एक जानकारी भेजें: रजिस्टर्ड ऑर्डर नंबर, ईमेल आईडी, "
               "मोबाइल नंबर, AWB / ट्रैकिंग नंबर — या ऑर्डर से जुड़ी कोई और जानकारी।"),
        "gu": ("તમારો ઓર્ડર શોધવા માટે થોડી વિગતો જોઈએ",
               "DeoDap ને લખવા બદલ આભાર. અમને આ મેલ પરથી તમારો ઓર્ડર મળ્યો નથી.\n\n"
               "કૃપા કરીને આમાંથી કોઈ એક મોકલો: રજિસ્ટર્ડ ઓર્ડર નંબર, ઈમેલ આઈડી, "
               "મોબાઈલ નંબર, AWB / ટ્રેકિંગ નંબર — અથવા ઓર્ડર સાથે જોડાયેલી કોઈ બીજી વિગત."),
    },
    # M2 — Route B evidence: unboxing video AND photo, mandatory.
    "M2": {
        "en": ("Photo & video required to register your complaint",
               "Sorry to hear this. To register {complaint_ref}, "
               "please reply with an unedited unboxing video AND a clear photo of the "
               "item.\n\nThese are mandatory for damage / wrong / missing-item claims."),
        "hi": ("शिकायत दर्ज करने के लिए फोटो और वीडियो आवश्यक है",
               "यह सुनकर दुख हुआ। {complaint_ref} दर्ज करने के लिए कृपया एक "
               "बिना एडिट किया हुआ अनबॉक्सिंग वीडियो और प्रोडक्ट की साफ फोटो भेजें।\n\n"
               "डैमेज / गलत / मिसिंग आइटम की शिकायत के लिए ये अनिवार्य हैं।"),
        "gu": ("ફરિયાદ નોંધવા માટે ફોટો અને વિડિયો જરૂરી છે",
               "આ સાંભળીને દુઃખ થયું. {complaint_ref} નોંધવા માટે કૃપા કરીને "
               "એડિટ ન કરેલો અનબોક્સિંગ વિડિયો અને પ્રોડક્ટનો સ્પષ્ટ ફોટો મોકલો.\n\n"
               "ડેમેજ / ખોટી / ગુમ વસ્તુની ફરિયાદ માટે આ ફરજિયાત છે."),
    },
    # M_CANCEL_LOOKUP — order cancellation: ask for the order reference (no evidence).
    "M_CANCEL_LOOKUP": {
        "en": ("Cancellation Request Received",
               "Hi,\n\nWe found your cancellation request.\n\n"
               "Please reply with any one of:\n"
               "  - Order Number\n  - AWB / Tracking Number\n  - Registered Email ID\n\n"
               "Our team will review your cancellation request."),
        "hi": ("रद्दीकरण अनुरोध प्राप्त हुआ",
               "नमस्ते,\n\nहमें आपका रद्दीकरण (cancellation) अनुरोध मिला है।\n\n"
               "कृपया इनमें से कोई एक भेजें:\n"
               "  - ऑर्डर नंबर\n  - AWB / ट्रैकिंग नंबर\n  - रजिस्टर्ड ईमेल आईडी\n\n"
               "हमारी टीम आपके रद्दीकरण अनुरोध की समीक्षा करेगी।"),
        "gu": ("રદ કરવાની વિનંતી મળી",
               "નમસ્તે,\n\nઅમને તમારી રદ (cancellation) કરવાની વિનંતી મળી છે.\n\n"
               "કૃપા કરીને આમાંથી કોઈ એક મોકલો:\n"
               "  - ઓર્ડર નંબર\n  - AWB / ટ્રેકિંગ નંબર\n  - રજિસ્ટર્ડ ઈમેલ આઈડી\n\n"
               "અમારી ટીમ તમારી રદ કરવાની વિનંતીની સમીક્ષા કરશે."),
    },
    # M_CANCEL_NOT_FOUND — order cancellation: the identifier the customer sent could NOT be
    # verified against Shopify / the courier -> ask for a VALID one. No ticket is created.
    "M_CANCEL_NOT_FOUND": {
        "en": ("We couldn't find this order",
               "We couldn't find this order number. Please check it and send a valid Order "
               "Number, AWB, or Registered Email."),
        "hi": ("हमें यह ऑर्डर नहीं मिला",
               "हमें यह ऑर्डर नंबर नहीं मिला। कृपया इसे जाँचें और एक सही ऑर्डर नंबर, AWB, या "
               "रजिस्टर्ड ईमेल भेजें।"),
        "gu": ("અમને આ ઓર્ડર મળ્યો નથી",
               "અમને આ ઓર્ડર નંબર મળ્યો નથી. કૃપા કરીને તેને તપાસો અને માન્ય ઓર્ડર નંબર, AWB, "
               "અથવા રજિસ્ટર્ડ ઈમેલ મોકલો."),
    },
    # M_TRACK_LOOKUP — Shipment Tracking STEP 2: no identifier in the email -> ask for ANY
    # ONE of Order Number / Mobile / Email. No Shopify call, no ticket, no link.
    "M_TRACK_LOOKUP": {
        "en": ("Order Status — please share your details",
               "Thank you for contacting DeoDap.\n\nTo check your order status, please reply "
               "with ANY ONE of the following:\n\n• Order Number\n• Registered Mobile Number\n"
               "• Registered Email ID\n\nOnce received, we will provide your latest tracking "
               "status."),
        "hi": ("ऑर्डर स्थिति — कृपया अपनी जानकारी भेजें",
               "DeoDap से संपर्क करने के लिए धन्यवाद।\n\nअपने ऑर्डर की स्थिति जाँचने के लिए कृपया "
               "इनमें से कोई एक भेजें:\n\n• ऑर्डर नंबर\n• रजिस्टर्ड मोबाइल नंबर\n• रजिस्टर्ड ईमेल आईडी\n\n"
               "प्राप्त होते ही हम आपको आपकी नवीनतम ट्रैकिंग स्थिति देंगे।"),
        "gu": ("ઓર્ડર સ્થિતિ — કૃપા કરીને તમારી વિગતો મોકલો",
               "DeoDap નો સંપર્ક કરવા બદલ આભાર.\n\nતમારા ઓર્ડરની સ્થિતિ તપાસવા માટે કૃપા કરીને "
               "આમાંથી કોઈ એક મોકલો:\n\n• ઓર્ડર નંબર\n• રજિસ્ટર્ડ મોબાઈલ નંબર\n• રજિસ્ટર્ડ ઈમેલ આઈડી\n\n"
               "મળતાં જ અમે તમને તમારી તાજેતરની ટ્રેકિંગ સ્થિતિ આપીશું."),
    },
    # M_TRACK_STATUS — Shipment Tracking STEP 5: order found. {details} (built in code) is
    # the Order ID / Status / Courier / AWB / live courier URL block.
    "M_TRACK_STATUS": {
        "en": ("Your Order Tracking Update",
               "Hi,\n\nHere is the latest status for your order:\n\n{details}\n\n"
               "Reply to this email if you need any more help."),
        "hi": ("आपके ऑर्डर की ट्रैकिंग अपडेट",
               "नमस्ते,\n\nआपके ऑर्डर की नवीनतम स्थिति यह है:\n\n{details}\n\n"
               "किसी और सहायता के लिए इस ईमेल का उत्तर दें।"),
        "gu": ("તમારા ઓર્ડરની ટ્રેકિંગ અપડેટ",
               "નમસ્તે,\n\nતમારા ઓર્ડરની તાજેતરની સ્થિતિ આ રહી:\n\n{details}\n\n"
               "વધુ મદદ માટે આ ઈમેલનો જવાબ આપો."),
    },
    # M_TRACK_NOT_FOUND — Shipment Tracking STEP 6: no order matched the provided details.
    "M_TRACK_NOT_FOUND": {
        "en": ("We could not locate your order",
               "We could not locate an order using the provided details.\n\nPlease verify "
               "and resend your:\n\n• Order Number\n• Registered Mobile Number\n"
               "• Registered Email ID"),
        "hi": ("हम आपका ऑर्डर नहीं ढूँढ सके",
               "दी गई जानकारी से हमें कोई ऑर्डर नहीं मिला।\n\nकृपया जाँच कर दोबारा भेजें:\n\n"
               "• ऑर्डर नंबर\n• रजिस्टर्ड मोबाइल नंबर\n• रजिस्टर्ड ईमेल आईडी"),
        "gu": ("અમે તમારો ઓર્ડર શોધી શક્યા નથી",
               "આપેલી વિગતોથી અમને કોઈ ઓર્ડર મળ્યો નથી.\n\nકૃપા કરીને ચકાસીને ફરી મોકલો:\n\n"
               "• ઓર્ડર નંબર\n• રજિસ્ટર્ડ મોબાઈલ નંબર\n• રજિસ્ટર્ડ ઈમેલ આઈડી"),
    },
    # M_TRACK_UNAVAILABLE — Shipment Tracking: the lookup hit an error (couldn't reach the
    # store). Not the same as 'not found'.
    "M_TRACK_UNAVAILABLE": {
        "en": ("Tracking Temporarily Unavailable",
               "Hi,\n\nWe're unable to fetch your tracking right now. Please try again in a "
               "little while -- sorry for the inconvenience."),
        "hi": ("ट्रैकिंग अस्थायी रूप से अनुपलब्ध",
               "नमस्ते,\n\nहम अभी आपकी ट्रैकिंग प्राप्त नहीं कर पा रहे हैं। कृपया थोड़ी देर बाद "
               "पुनः प्रयास करें -- असुविधा के लिए क्षमा करें।"),
        "gu": ("ટ્રેકિંગ થોડા સમય માટે અનુપલબ્ધ",
               "નમસ્તે,\n\nઅમે હાલ તમારી ટ્રેકિંગ મેળવી શકતા નથી. કૃપા કરીને થોડી વાર પછી ફરી "
               "પ્રયાસ કરો -- અસુવિધા બદલ માફ કરશો."),
    },
    # M_VERIFY_REQUEST — Inquiry (Franchise / Dropshipping / Company Profile / Invoice)
    # STEP 1: first email acknowledgement asking for an identifier. NO ticket yet.
    "M_VERIFY_REQUEST": {
        "en": ("Request Received",
               "Hi,\n\nThank you for contacting DeoDap.\n\nWe have received your request.\n\n"
               "To proceed, please reply to this email with:\n\n"
               "• Order Number (for Invoice requests)\nOR\n• Mobile Number\nOR\n"
               "• Registered Email ID\n\n"
               "After verification, our team will process your request."),
        "hi": ("अनुरोध प्राप्त हुआ",
               "नमस्ते,\n\nDeoDap से संपर्क करने के लिए धन्यवाद।\n\nहमें आपका अनुरोध मिल गया है।\n\n"
               "आगे बढ़ने के लिए कृपया इस ईमेल का उत्तर इनमें से किसी एक के साथ दें:\n\n"
               "• ऑर्डर नंबर (इनवॉइस अनुरोध के लिए)\nया\n• मोबाइल नंबर\nया\n"
               "• रजिस्टर्ड ईमेल आईडी\n\n"
               "सत्यापन के बाद हमारी टीम आपके अनुरोध पर कार्य करेगी।"),
        "gu": ("વિનંતી મળી",
               "નમસ્તે,\n\nDeoDap નો સંપર્ક કરવા બદલ આભાર.\n\nઅમને તમારી વિનંતી મળી છે.\n\n"
               "આગળ વધવા માટે કૃપા કરીને આ ઈમેલનો જવાબ આમાંથી કોઈ એક સાથે આપો:\n\n"
               "• ઓર્ડર નંબર (ઇન્વોઇસ વિનંતી માટે)\nઅથવા\n• મોબાઈલ નંબર\nઅથવા\n"
               "• રજિસ્ટર્ડ ઈમેલ આઈડી\n\n"
               "ચકાસણી પછી અમારી ટીમ તમારી વિનંતી પર કાર્ય કરશે."),
    },
    # M_VERIFY_FAILED — Inquiry STEP 4: the provided details could not be verified.
    "M_VERIFY_FAILED": {
        "en": ("We could not verify your details",
               "We could not verify the provided information.\n\n"
               "Please reply with a valid:\n• Order Number\n• Mobile Number\n"
               "• Registered Email ID"),
        "hi": ("हम आपकी जानकारी सत्यापित नहीं कर सके",
               "दी गई जानकारी हम सत्यापित नहीं कर सके।\n\n"
               "कृपया एक मान्य जानकारी भेजें:\n• ऑर्डर नंबर\n• मोबाइल नंबर\n• रजिस्टर्ड ईमेल आईडी"),
        "gu": ("અમે તમારી વિગતો ચકાસી શક્યા નથી",
               "આપેલી માહિતી અમે ચકાસી શક્યા નથી.\n\n"
               "કૃપા કરીને માન્ય માહિતી મોકલો:\n• ઓર્ડર નંબર\n• મોબાઈલ નંબર\n• રજિસ્ટર્ડ ઈમેલ આઈડી"),
    },
    # M2P — photo required, video optional (Damaged / quality issues).
    "M2P": {
        "en": ("Photo required to register your complaint",
               "Sorry to hear this. To register {complaint_ref}, "
               "please reply with a clear photo of the item showing the issue.\n\n"
               "A short video helps but is not required for this type of complaint."),
        "hi": ("शिकायत दर्ज करने के लिए फोटो आवश्यक है",
               "यह सुनकर दुख हुआ। {complaint_ref} दर्ज करने के लिए कृपया "
               "समस्या दिखाते हुए प्रोडक्ट की एक साफ फोटो भेजें।\n\n"
               "इस प्रकार की शिकायत के लिए वीडियो सहायक है पर अनिवार्य नहीं।"),
        "gu": ("ફરિયાદ નોંધવા માટે ફોટો જરૂરી છે",
               "આ સાંભળીને દુઃખ થયું. {complaint_ref} નોંધવા માટે કૃપા કરીને "
               "સમસ્યા દર્શાવતો પ્રોડક્ટનો સ્પષ્ટ ફોટો મોકલો.\n\n"
               "આ પ્રકારની ફરિયાદ માટે વિડિયો મદદરૂપ છે પણ ફરજિયાત નથી."),
    },
    # MPAY — Payment Issue (payment deducted but order not placed): PAYMENT SCREENSHOT only.
    # NEVER ask for a "photo of the item" -- there is no item; it is a transaction dispute.
    "MPAY": {
        "en": ("Payment screenshot required to investigate",
               "Please upload:\n• Payment Screenshot (Mandatory)\n\n"
               "This helps us verify the transaction and investigate the issue."),
        "hi": ("जांच के लिए पेमेंट स्क्रीनशॉट आवश्यक है",
               "कृपया अपलोड करें:\n• पेमेंट स्क्रीनशॉट (अनिवार्य)\n\n"
               "इससे हमें लेनदेन सत्यापित करने और समस्या की जांच करने में मदद मिलती है।"),
        "gu": ("તપાસ માટે પેમેન્ટ સ્ક્રીનશોટ જરૂરી છે",
               "કૃપા કરીને અપલોડ કરો:\n• પેમેન્ટ સ્ક્રીનશોટ (ફરજિયાત)\n\n"
               "આ અમને વ્યવહાર ચકાસવા અને સમસ્યાની તપાસ કરવામાં મદદ કરે છે."),
    },
    # --- Delivered-Item evidence requests (exact per-concern subject + body). The concern is
    # detected by evidence.delivered_evidence_case(); _send_delivered_evidence_request selects the
    # template via DELIVERED_EVIDENCE_RULES[case]["mail"] and uses the TEMPLATE's subject + body.
    # English-only (spec wording); the "Regards, DeoDap Support Team" signature is appended by
    # render(), so bodies stop before it. ------------------------------------------------------- #
    "EV_DAMAGED": {
        "en": ("DeoDap Support | Damaged Product - Evidence Required",
               "Dear Customer,\n\n"
               "We are sorry to hear that you received a damaged product.\n\n"
               "To process your request, please reply with:\n\n"
               "• Unboxing video (without cuts) – Mandatory\n"
               "• Clear images of the damaged product – Mandatory\n\n"
               "Once we receive the required evidence, our support team will review your request "
               "and assist you further."),
    },
    "EV_NON_WORKING": {
        "en": ("DeoDap Support | Non-Working Product - Troubleshooting",
               "Dear Customer,\n\n"
               "We are sorry to hear that your product is not working.\n\n"
               "Before proceeding, please charge the product for 3–4 hours and try using it "
               "again.\n\n"
               "If the issue still persists, please reply with:\n\n"
               "• A clear video showing that the product is not working\n\n"
               "Our support team will review your request and assist you further."),
    },
    "EV_MISSING": {
        "en": ("DeoDap Support | Missing Product - Evidence Required",
               "Dear Customer,\n\n"
               "We are sorry to hear that an item is missing from your order.\n\n"
               "Please reply with:\n\n"
               "• Unboxing video (without cuts) – Mandatory\n"
               "• Image of the POS paper – Mandatory"),
    },
    "EV_WRONG_PRODUCT": {
        "en": ("DeoDap Support | Wrong Product Received - Evidence Required",
               "Dear Customer,\n\n"
               "We are sorry to hear that you received the wrong product.\n\n"
               "Please reply with:\n\n"
               "• Unboxing video (without cuts) – Mandatory\n"
               "• Clear images of the wrong product received\n"
               "• SKU of the wrong product received"),
    },
    "EV_WRONG_PARCEL": {
        "en": ("DeoDap Support | Wrong Parcel Received - Evidence Required",
               "Dear Customer,\n\n"
               "We are sorry to hear that you received the wrong parcel.\n\n"
               "Please reply with:\n\n"
               "• Image of the POS paper\n"
               "• Clear images of all products received\n"
               "• Product count/quantity received\n"
               "• Image of the shipping label available on the package"),
    },
    "EV_DEFECTIVE": {
        "en": ("DeoDap Support | Defective Product - Evidence Required",
               "Dear Customer,\n\n"
               "We are sorry to hear that you received a defective product.\n\n"
               "Please reply with:\n\n"
               "• Clear images showing the defect\n"
               "• A video clearly demonstrating the defect (if applicable)"),
    },
    # (M3 "order# + phone required" was REMOVED -- order id / phone no longer block
    #  ticket creation, so no such request is ever sent.)
    # M4 — Route A answer; closes the request (reply to reopen).
    "M4": {
        "en": ("Update on your DeoDap request",
               "{answer}\n\nThis mail closes your request — simply reply to reopen it."),
        "hi": ("आपके DeoDap अनुरोध पर अपडेट",
               "{answer}\n\nयह मेल आपके अनुरोध को बंद कर देता है — दोबारा खोलने के लिए बस "
               "रिप्लाई करें।"),
        "gu": ("તમારી DeoDap વિનંતી પર અપડેટ",
               "{answer}\n\nઆ મેલ તમારી વિનંતી બંધ કરે છે — ફરી ખોલવા માટે ફક્ત રિપ્લાય કરો."),
    },
    # M5 — new ticket created (with the Care Panel ticket URL). {tracking_url} is included
    # ONLY when Care Panel creation succeeded (a real care.deodap.in hash exists).
    "M5": {
        "en": ("Support Ticket Created Successfully",
               "Your complaint is registered.\n\nTicket ID: {ticket_number}\n\n"
               "View Ticket:\n{tracking_url}\n\n"
               "Our team will update you on this same ticket."),
        "hi": ("सपोर्ट टिकट सफलतापूर्वक बनाया गया",
               "आपकी शिकायत दर्ज हो गई है।\n\nटिकट आईडी: {ticket_number}\n\n"
               "टिकट देखें:\n{tracking_url}\n\n"
               "हमारी टीम इसी टिकट पर आपको अपडेट देगी।"),
        "gu": ("સપોર્ટ ટિકિટ સફળતાપૂર્વક બની",
               "તમારી ફરિયાદ નોંધાઈ ગઈ છે.\n\nટિકિટ આઈડી: {ticket_number}\n\n"
               "ટિકિટ જુઓ:\n{tracking_url}\n\n"
               "અમારી ટીમ આ જ ટિકિટ પર તમને અપડેટ આપશે."),
    },
    # M5N — new ticket created, but no Care Panel tracking link available.
    "M5N": {
        "en": ("Support Ticket Created Successfully",
               "We have received your request and created a support ticket.\n\n"
               "Ticket ID: {ticket_number}\n\n"
               "Our support team will review your request and contact you shortly."),
        "hi": ("सपोर्ट टिकट सफलतापूर्वक बनाया गया",
               "हमें आपका अनुरोध मिल गया है और एक सपोर्ट टिकट बना दिया गया है।\n\n"
               "टिकट आईडी: {ticket_number}\n\n"
               "हमारी टीम आपके अनुरोध की समीक्षा कर शीघ्र संपर्क करेगी।"),
        "gu": ("સપોર્ટ ટિકિટ સફળતાપૂર્વક બની",
               "અમને તમારી વિનંતી મળી છે અને એક સપોર્ટ ટિકિટ બનાવી છે.\n\n"
               "ટિકિટ આઈડી: {ticket_number}\n\n"
               "અમારી ટીમ તમારી વિનંતીની સમીક્ષા કરી જલ્દી સંપર્ક કરશે."),
    },
    # M5_INQUIRY — verified two-step inquiry (invoice / franchise / dropship / company)
    # ticket created, WITH the Care Panel link. {registered_line} is the category-specific
    # sentence (see _INQUIRY_REGISTERED_LINE in service.py).
    "M5_INQUIRY": {
        "en": ("Request Registered Successfully",
               "Hi,\n\n{registered_line}\n\nTicket ID: {ticket_number}\n\n"
               "View Ticket:\n{tracking_url}"),
        "hi": ("अनुरोध सफलतापूर्वक दर्ज किया गया",
               "नमस्ते,\n\n{registered_line}\n\nटिकट आईडी: {ticket_number}\n\n"
               "टिकट देखें:\n{tracking_url}"),
        "gu": ("વિનંતી સફળતાપૂર્વક નોંધાઈ",
               "નમસ્તે,\n\n{registered_line}\n\nટિકિટ આઈડી: {ticket_number}\n\n"
               "ટિકિટ જુઓ:\n{tracking_url}"),
    },
    # M5_INQUIRY_N — verified inquiry ticket created, but no Care Panel link available.
    "M5_INQUIRY_N": {
        "en": ("Request Registered Successfully",
               "Hi,\n\n{registered_line}\n\nTicket ID: {ticket_number}\n\n"
               "Our support team will review your request and contact you shortly."),
        "hi": ("अनुरोध सफलतापूर्वक दर्ज किया गया",
               "नमस्ते,\n\n{registered_line}\n\nटिकट आईडी: {ticket_number}\n\n"
               "हमारी टीम आपके अनुरोध की समीक्षा कर शीघ्र संपर्क करेगी।"),
        "gu": ("વિનંતી સફળતાપૂર્વક નોંધાઈ",
               "નમસ્તે,\n\n{registered_line}\n\nટિકિટ આઈડી: {ticket_number}\n\n"
               "અમારી ટીમ તમારી વિનંતીની સમીક્ષા કરી જલ્દી સંપર્ક કરશે."),
    },
    # M6N — existing ticket updated, but no Care Panel tracking link available.
    "M6N": {
        "en": ("Ticket Updated Successfully",
               "We have received your additional information and updated your existing "
               "support ticket.\n\nTicket ID: {ticket_number}\n\n"
               "Our support team is reviewing it and will contact you shortly."),
        "hi": ("टिकट सफलतापूर्वक अपडेट किया गया",
               "हमें आपकी अतिरिक्त जानकारी मिल गई है और आपका मौजूदा सपोर्ट टिकट अपडेट कर "
               "दिया गया है।\n\nटिकट आईडी: {ticket_number}\n\n"
               "हमारी टीम इसकी समीक्षा कर रही है और शीघ्र संपर्क करेगी।"),
        "gu": ("ટિકિટ સફળતાપૂર્વક અપડેટ થઈ",
               "અમને તમારી વધારાની માહિતી મળી છે અને તમારી હાલની સપોર્ટ ટિકિટ અપડેટ કરી "
               "છે.\n\nટિકિટ આઈડી: {ticket_number}\n\n"
               "અમારી ટીમ તેની સમીક્ષા કરી રહી છે અને જલ્દી સંપર્ક કરશે."),
    },
    # M6 — ticket already open (same issue) -> appended.
    "M6": {
        "en": ("Existing Ticket Found",
               "A ticket for this issue is already open on your order: "
               "{ticket_number}.\n\nWe have added today's details to it.\n\n"
               "Track the latest update here: {tracking_url}"),
        "hi": ("मौजूदा टिकट मिला",
               "इस समस्या के लिए आपके ऑर्डर पर पहले से एक टिकट खुला है: {ticket_number}।\n\n"
               "हमने आज की जानकारी उसमें जोड़ दी है।\n\n"
               "नवीनतम अपडेट यहाँ ट्रैक करें: {tracking_url}"),
        "gu": ("હાલની ટિકિટ મળી",
               "આ સમસ્યા માટે તમારા ઓર્ડર પર પહેલેથી એક ટિકિટ ખુલ્લી છે: {ticket_number}.\n\n"
               "અમે આજની વિગતો તેમાં ઉમેરી છે.\n\n"
               "નવીનતમ અપડેટ અહીં ટ્રેક કરો: {tracking_url}"),
    },
    # M7R — 24h reminder on a waiting state.
    "M7R": {
        "en": ("Reminder: we're waiting to proceed with your request",
               "We're still waiting for {missing} to proceed with your request.\n\n"
               "Please reply and we'll continue right away."),
        "hi": ("रिमाइंडर: हम आपके अनुरोध को आगे बढ़ाने के लिए प्रतीक्षा कर रहे हैं",
               "आपके अनुरोध को आगे बढ़ाने के लिए हम अभी भी {missing} की प्रतीक्षा कर रहे हैं।\n\n"
               "कृपया रिप्लाई करें और हम तुरंत आगे बढ़ेंगे।"),
        "gu": ("રિમાઇન્ડર: અમે તમારી વિનંતી આગળ વધારવા રાહ જોઈ રહ્યા છીએ",
               "તમારી વિનંતી આગળ વધારવા માટે અમે હજુ {missing} ની રાહ જોઈ રહ્યા છીએ.\n\n"
               "કૃપા કરીને રિપ્લાય કરો અને અમે તરત આગળ વધીશું."),
    },
    # M7C — 72h auto-close; reply within 7 days reopens automatically.
    "M7C": {
        "en": ("Closing your request for now",
               "We're closing this request for now as we did not receive the required "
               "details.\n\nReply anytime within 7 days and it will reopen "
               "automatically."),
        "hi": ("आपका अनुरोध फिलहाल बंद किया जा रहा है",
               "आवश्यक जानकारी न मिलने के कारण हम यह अनुरोध फिलहाल बंद कर रहे हैं।\n\n"
               "7 दिनों के भीतर कभी भी रिप्लाई करें और यह अपने आप फिर से खुल जाएगा।"),
        "gu": ("તમારી વિનંતી હાલ માટે બંધ કરી રહ્યા છીએ",
               "જરૂરી વિગતો ન મળતાં અમે આ વિનંતી હાલ માટે બંધ કરી રહ્યા છીએ.\n\n"
               "7 દિવસની અંદર ગમે ત્યારે રિપ્લાય કરો અને તે આપમેળે ફરી ખુલશે."),
    },
}


def normalize_lang(lang):
    """Map a detected language code to a supported template language (en/hi/gu)."""
    code = (lang or "").strip().lower()[:2]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def render(mail_id, lang=DEFAULT_LANG, **vars):
    """Return (subject, body) for a mail id in the customer's language.

    Body always ends with the localized signature. Missing placeholders render as
    an empty string (so a partial var set never raises).
    """
    code = normalize_lang(lang)
    variants = MAILS.get(mail_id)
    if not variants:
        raise KeyError(f"Unknown mail id: {mail_id}")
    subject, raw_body = variants.get(code) or variants[DEFAULT_LANG]
    safe = defaultdict(str, {k: ("" if v is None else v) for k, v in vars.items()})
    body = raw_body.format_map(safe)
    return subject, f"{body}\n\n{SIGN[code]}"
