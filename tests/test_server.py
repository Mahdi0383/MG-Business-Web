            "id": "pc-01", "name": "PC-01", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": False
        }, token)
        self.assertEqual(status, 201)
        # Device updates must be handled only by POST; GET must never attempt a write.
        status, result = self.request("GET", "/api/gamenet/devices/pc-01", token=token)
        self.assertEqual(status, 404)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-01", "deviceId": "pc-01", "customerId": "customer-1", "playersCount": 2
        }, token)
        self.assertEqual(status, 201)
        status, retry_session = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-01","deviceId":"pc-01","customerId":"customer-1","playersCount":2
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_session["duplicate"])
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-02", "deviceId": "pc-01", "playersCount": 1
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("POST", "/api/gamenet/devices", {
            "id":"pc-02","name":"PC-02","deviceType":"pc","hourlyRate":180000,"isFree":False
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/devices/pc-01", {
            "name": "PC-01 Updated", "deviceType": "pc", "hourlyRate": 130000, "isFree": False
        }, token)
        self.assertEqual(status, 200)
        db = server.connect()
        try:
            old_start=int(__import__('time').time())-1200
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-01'", (old_start,))
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01'", (old_start,))
            db.commit()
        finally:
            db.close()
        status, invalid_adjust = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "playersCount":0
        }, token)
        self.assertEqual(status,400)
        # Moving a running session must not bypass an active reservation for another customer.
        from datetime import datetime, timezone, timedelta
        reservation_start=datetime.now(timezone.utc).isoformat(timespec="seconds")
        reservation_end=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec="seconds")
        status, reserved_device = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-blocking-transfer","deviceId":"pc-02",
            "startsAt":reservation_start,"endsAt":reservation_end,"playersCount":1
        }, token)
        self.assertEqual(status,201)
        status, blocked_transfer = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "deviceId":"pc-02","playersCount":3,"isFree":True
        }, token)
        self.assertEqual(status,409)
        self.assertIn("رزرو", blocked_transfer["error"])
        status, cancelled_reservation = self.request("POST",
            "/api/gamenet/reservations/reservation-blocking-transfer/cancel", {}, token)
        self.assertEqual(status,200)
        status, moved = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "deviceId":"pc-02","playersCount":3,"isFree":True
        }, token)
        self.assertEqual(status,200)
        self.assertEqual(moved["deviceId"],"pc-02")
        self.assertEqual(moved["playersCount"],3)
        self.assertTrue(moved["isFree"])
        db = server.connect()
        try:
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01' AND ended_at IS NULL",
                (int(__import__('time').time())-600,))
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result["paymentMethod"],"credit")
        self.assertGreaterEqual(result["amountDue"], 39000)
        self.assertLess(result["amountDue"], 43000)
        game_charge = result["amountDue"]
        status, retry_end = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_end["duplicate"])
        self.assertEqual(retry_end["amountDue"],game_charge)
        self.assertEqual(retry_end["paymentMethod"],"credit")
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(result["items"][0]["name"], "PC-01 Updated")
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "customer-1")["balance"], 35000 + game_charge)
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["status"], "ended")
        status, segments = self.request("GET","/api/gamenet/sessions/session-01/segments",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(segments["items"]),2)
        self.assertEqual(segments["items"][0]["deviceId"],"pc-01")
        self.assertEqual(segments["items"][1]["deviceId"],"pc-02")
        self.assertEqual(segments["items"][0]["playersCount"],2)
        self.assertEqual(segments["items"][1]["playersCount"],3)
        self.assertTrue(segments["items"][1]["isFree"])
        status, game_backup = self.request("GET","/api/backup",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(game_backup["tables"]["game_session_segments"]),2)

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-free", "name": "Free PC", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": True
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-free", "deviceId": "pc-free", "playersCount": 1
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions/session-free/end", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result["amountDue"], 0)
        status, result = self.request("POST", "/api/gamenet/devices", {
            "id":"pc-card","name":"Card PC","deviceType":"pc","hourlyRate":120000,"isFree":False
        },token)
        self.assertEqual(status,201)
        status, result = self.request("POST","/api/gamenet/sessions",{
            "id":"session-card","deviceId":"pc-card","customerId":"customer-1","playersCount":1
        },token)
        self.assertEqual(status,201)
        db=server.connect()
        try:
            card_start=int(__import__('time').time())-120
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-card'",(card_start,))
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-card'",(card_start,))
            db.commit()
        finally:
            db.close()
        status, card_settlement = self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"card"
        },token)
        self.assertEqual(status,200)
        self.assertEqual(card_settlement["paymentMethod"],"card")
        self.assertGreater(card_settlement["amountDue"],0)
        status, card_retry = self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"credit"
        },token)