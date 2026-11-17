            if path.startswith("/api/gamenet/sessions/") and path.endswith("/adjust"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id=path.split("/")[-2]
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error":"دسترسی تغییر نشست بازی ندارید."})
                requested_device=data.get("deviceId")
                requested_players=data.get("playersCount")
                requested_free=data.get("isFree")
                if requested_device is not None and (not isinstance(requested_device,str) or not 1<=len(requested_device)<=100):
                    return self.send_json(400, {"error":"شناسه دستگاه معتبر نیست."})
                if requested_players is not None and (type(requested_players) is not int or not 1<=requested_players<=16):
                    return self.send_json(400, {"error":"تعداد بازیکنان باید بین ۱ تا ۱۶ باشد."})
                if requested_free is not None and type(requested_free) is not bool:
                    return self.send_json(400, {"error":"حالت رایگان/پولی معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                session=db.execute("SELECT * FROM game_sessions WHERE id=? AND tenant_id=?",
                    (session_id,user["tenant_id"])).fetchone()
                if not session:
                    db.rollback(); return self.send_json(404, {"error":"نشست پیدا نشد."})
                if session["status"]!="running":
                    db.rollback(); return self.send_json(409, {"error":"فقط نشست در حال اجرا قابل تغییر است."})
                target_id=requested_device or session["device_id"]
                target=db.execute("SELECT * FROM game_devices WHERE id=? AND tenant_id=? AND active=1",
                    (target_id,user["tenant_id"])).fetchone()
                if not target:
                    db.rollback(); return self.send_json(404, {"error":"دستگاه مقصد فعال پیدا نشد."})
                occupied=db.execute("""SELECT id FROM game_sessions
                    WHERE tenant_id=? AND device_id=? AND status='running' AND id<>?""",
                    (user["tenant_id"],target_id,session_id)).fetchone()
                if occupied:
                    db.rollback(); return self.send_json(409, {"error":"دستگاه مقصد در حال حاضر میزبان نشست دیگری است."})
                players=requested_players if requested_players is not None else session["players_count"]
                changed_device=target_id!=session["device_id"]
                now=int(time.time())
                if changed_device:
                    active_reservation=db.execute("""SELECT customer_id FROM gamenet_reservations
                        WHERE tenant_id=? AND device_id=? AND status='booked'
                        AND starts_at<=? AND ends_at>? ORDER BY starts_at LIMIT 1""",
                        (user["tenant_id"],target_id,now,now)).fetchone()
                    if active_reservation and active_reservation["customer_id"]!=session["customer_id"]:
                        db.rollback()
                        return self.send_json(409, {"error":"دستگاه مقصد در حال حاضر برای مشتری دیگری رزرو شده است."})
                rate=target["hourly_rate"] if changed_device else session["rate_per_hour"]
                free=target["is_free"] if changed_device else session["is_free"]
                if requested_free is not None: free=int(requested_free)
                if target_id==session["device_id"] and players==session["players_count"] and rate==session["rate_per_hour"] and int(free)==session["is_free"]:
                    db.commit()
                    return self.send_json(200, {"ok":True,"unchanged":True,"deviceId":target_id,
                        "playersCount":players,"ratePerHour":rate,"isFree":bool(free)})
                active_segment=db.execute("""SELECT id FROM game_session_segments
                    WHERE tenant_id=? AND session_id=? AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1""",(user["tenant_id"],session_id)).fetchone()
                if not active_segment:
                    db.rollback(); return self.send_json(409, {"error":"بخش فعال صورتحساب نشست پیدا نشد."})
                db.execute("UPDATE game_session_segments SET ended_at=? WHERE id=? AND ended_at IS NULL",
                    (max(now,session["started_at"]),active_segment["id"]))
                db.execute("""INSERT INTO game_session_segments
                    (id,tenant_id,session_id,device_id,started_at,players_count,rate_per_hour,is_free)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (secrets.token_urlsafe(18),user["tenant_id"],session_id,target_id,now,players,rate,int(free)))
                db.execute("""UPDATE game_sessions SET device_id=?,players_count=?,rate_per_hour=?,is_free=?
                    WHERE id=? AND tenant_id=? AND status='running'""",
                    (target_id,players,rate,int(free),session_id,user["tenant_id"]))
                audit(db,user["tenant_id"],user["id"],"gamenet.session.adjusted","game_session",session_id,
                    {"deviceId":target_id,"playersCount":players,"ratePerHour":rate,"isFree":bool(free)})
                db.commit()
                return self.send_json(200, {"ok":True,"deviceId":target_id,"deviceName":target["name"],
                    "playersCount":players,"ratePerHour":rate,"isFree":bool(free),"changedAt":now})
            if path.startswith("/api/gamenet/sessions/") and path.endswith("/end"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id = path.split("/")[-2]
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی پایان نشست بازی ندارید."})
                db.execute("BEGIN IMMEDIATE")
                session = db.execute("SELECT * FROM game_sessions WHERE id=? AND tenant_id=?",
                                     (session_id,user["tenant_id"])).fetchone()
                if not session:
                    db.rollback()
                    return self.send_json(404, {"error": "نشست پیدا نشد."})
                if session["status"]=="ended":
                    elapsed=max(0,int(session["ended_at"] or session["started_at"])-session["started_at"])
                    db.commit()
                    return self.send_json(200,{"ok":True,"amountDue":session["amount_due"],
                        "elapsedSeconds":elapsed,"paymentMethod":session["payment_method"],"duplicate":True})
                if session["status"]!="running":
                    db.rollback()
                    return self.send_json(409,{"error":"این نشست قابل پایان‌دادن نیست."})
                payment_method=data.get("paymentMethod","credit" if session["customer_id"] else "cash")
                if payment_method not in ("cash","card","online","credit"):
                    db.rollback()
                    return self.send_json(400,{"error":"روش تسویه نشست معتبر نیست."})
                if payment_method=="credit" and not session["customer_id"]:
                    db.rollback()
                    return self.send_json(400,{"error":"برای تسویه اعتباری باید مشتری نشست مشخص باشد."})
                now = int(time.time())
                elapsed = max(0,now-session["started_at"])
                db.execute("""UPDATE game_session_segments SET ended_at=?
                    WHERE tenant_id=? AND session_id=? AND ended_at IS NULL""",
                    (max(now,session["started_at"]),user["tenant_id"],session_id))
                segments=db.execute("""SELECT started_at,ended_at,rate_per_hour,is_free
                    FROM game_session_segments WHERE tenant_id=? AND session_id=? ORDER BY started_at,id""",
                    (user["tenant_id"],session_id)).fetchall()