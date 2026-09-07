# Stage 5 — Mobile Ready

## تم إنجازه فعليًا
- PWA installable shell: manifest + service worker + standalone mobile experience.
- Android native source project.
- Android Share intent: user can share text from another app into «محتاج إيه؟».
- Android NotificationListenerService skeleton: only after explicit OS permission.
- Mobile ingestion API with duplicate hashing.
- Notification/share events enter the same conservative follow-up detector.
- Default remains Suggest Only; the mobile layer does not auto-create follow-up work silently.
- Docker files for a persistent HTTPS-capable backend deployment target.

## ما لا أستطيع تسميته APK الآن
بيئة الإنشاء الحالية لا تحتوي Android SDK/Gradle toolchain اللازمة لإخراج APK موقّع/قابل للتثبيت.
لذلك تم إنشاء Android project source كامل بدل ادعاء إنتاج APK لم يتم بناؤه.

## المطلوب قبل APK Live
1. نشر backend على HTTPS domain.
2. وضع عنوانه في `android_app/app/build.gradle` بدل `https://CHANGE-ME.example.com`.
3. بناء APK/AAB باستخدام Android SDK/Gradle.
4. توقيع النسخة.
5. اختبار Notification Access وShare على جهاز فعلي.
6. توصيل WhatsApp/payment credentials بشكل منفصل عندما تتوفر.

## قاعدة الخصوصية
Notification Access ليس مفعّلًا تلقائيًا. المستخدم يفتحه بنفسه من إعدادات Android، والـbackend يقرر هل الحدث يستحق اقتراح متابعة قبل إزعاج المستخدم.
