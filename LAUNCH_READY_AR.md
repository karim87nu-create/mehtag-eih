# حزمة التشغيل الحي — أقصى ما يمكن إقفاله بدون حساب استضافة/دفع/WhatsApp

## الذي تم إقفاله الآن
- نسخة Android لم تعد تحتاج Backend URL مكتوب داخل الكود.
- عند أول فتح، التطبيق يطلب رابط HTTPS مرة واحدة ويحفظه على الجهاز.
- Share من أي تطبيق يفتح نفس رحلة «تابعها معايا».
- Notification Listener يستخدم نفس رابط الـBackend الذي أدخله المستخدم.
- PWA موجودة كخيار تثبيت سريع.
- Backend مجهز Docker + health endpoint + volume للبيانات.
- أضيف Workflow جاهز على GitHub Actions لبناء APK فعلي تلقائيًا.
- أضيف ملف نشر `render.yaml` و`.env.example`.

## بناء APK
لو المشروع موجود داخل Repository على GitHub:
1. Workflow اسمه `Build Android APK`.
2. GitHub يشغل Android SDK + Gradle 8.9.
3. يبني `app-debug.apk`.
4. يرفعه كـArtifact باسم `mehtag-eih-android-debug`.

لا يحتاج جهازك أن يحتوي Android Studio.

## لماذا لم أرسل APK في هذه اللحظة؟
حاولت بناءه داخل بيئة العمل الحالية، لكن هذه البيئة لا تحتوي Android SDK/Gradle ولا تسمح بتنزيل حزم Android الثنائية مباشرة.
كما أن GitHub المتصل بالحساب حاليًا لا يعرض أي Repository أقدر أرفع المشروع إليه لتشغيل Actions.
لذلك أغلقت مسار البناء نفسه بدل الادعاء أن APK بُني.

## أول تشغيل فعلي
بعد نشر الـBackend على HTTPS:
- ثبت APK.
- أدخل رابط الـBackend مرة واحدة.
- افتح التطبيق.
- Share يعمل مباشرة.
- Notification Access يظل اختيارًا صريحًا من إعدادات Android.

## ما يزال يحتاج حساب طرف خارجي وليس كودًا فقط
- Domain/hosting حي.
- حساب WhatsApp Business Platform أو Provider مناسب للتواصل المسموح.
- Payment provider + settlement/KYC.
- توقيع Release APK/AAB قبل متجر Google Play.

## ترتيب التشغيل الحقيقي
Backend HTTPS → APK → Share/notifications → أول 20 مستخدم → قناة تاجر حقيقية → دفع حقيقي.
