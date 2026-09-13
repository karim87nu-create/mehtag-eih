from app.intent_router import Route, route_turn, semantic_decision


def test_required_arabic_routes():
    cases = {
        "ايه الفرق بين الجديد والمستعمل؟": Route.FACTUAL_QUESTION,
        "رشحلي بين اتنين": Route.COMPARISON_RECOMMENDATION,
        "هو ده كويس؟": Route.COMPARISON_RECOMMENDATION,
        "انا متردد": Route.COMPARISON_RECOMMENDATION,
        "عايز اعرف الاول مش اشتري": Route.FACTUAL_QUESTION,
        "فين الطلب اللي عملناه": Route.REQUEST_STATUS_FOLLOWUP,
        "بص": Route.CASUAL_CHAT,
        "شكرا": Route.CASUAL_CHAT,
        "عايز صور rkv250": Route.MEDIA_IMAGE_REQUEST,
        "صورال RKV250": Route.MEDIA_IMAGE_REQUEST,
    }
    for text, expected in cases.items():
        assert route_turn(text).route == expected


def test_motorcycle_draft_routes_without_executing_early():
    assert route_turn("عايز موتوسيكل").route == Route.REQUEST_SEED
    assert route_turn("rkv250", draft_exists=True).route == Route.REQUEST_DETAIL
    assert route_turn("مستعمل", draft_exists=True).route == Route.REQUEST_DETAIL
    assert route_turn("في مصر الجديدة", draft_exists=True).route == Route.REQUEST_DETAIL
    assert route_turn("ابدأ", draft_exists=True).route == Route.REQUEST_EXECUTE


def test_questions_and_chat_never_become_draft_details():
    for text in ("ايه الفرق بين الجديد والمستعمل؟", "هو ده كويس؟", "انا متردد", "بص", "شكرا"):
        assert route_turn(text, draft_exists=True).route != Route.REQUEST_DETAIL


def test_semantic_decision_validates_model_output():
    result = semantic_decision({
        "route": "comparison_recommendation", "confidence": 0.91,
        "fits_active_draft": False, "execute_now": False,
    })
    assert result is not None
    assert result.route == Route.COMPARISON_RECOMMENDATION
    assert result.confidence == 0.91
    assert result.deterministic is False
    assert semantic_decision({"route": "invented", "confidence": 1}) is None
