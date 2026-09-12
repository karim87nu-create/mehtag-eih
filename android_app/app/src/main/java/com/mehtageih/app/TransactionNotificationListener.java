package com.mehtageih.app;

import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.app.Notification;
import android.os.Bundle;
import android.content.SharedPreferences;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

public class TransactionNotificationListener extends NotificationListenerService {
    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        SharedPreferences prefs=getSharedPreferences("app",MODE_PRIVATE);
        String backend=prefs.getString("backend_url","");
        String customer=prefs.getString("customer_ref","");
        if(backend.isEmpty() || customer.isEmpty()) return;
        Notification n=sbn.getNotification();
        Bundle e=n.extras;
        String title=String.valueOf(e.getCharSequence(Notification.EXTRA_TITLE,""));
        String body=String.valueOf(e.getCharSequence(Notification.EXTRA_TEXT,""));
        if(body.isBlank()) return;
        new Thread(() -> postEvent(backend,customer,sbn.getPackageName(),title,body)).start();
    }
    private void postEvent(String backend,String customer,String pkg,String title,String body){
        try{
            URL url=new URL(backend+"/api/mobile/source");
            HttpURLConnection c=(HttpURLConnection)url.openConnection();
            c.setRequestMethod("POST"); c.setDoOutput(true); c.setConnectTimeout(10000); c.setReadTimeout(10000);
            c.setRequestProperty("Content-Type","application/x-www-form-urlencoded; charset=UTF-8");
            c.setRequestProperty("X-Customer-Ref",customer);
            c.setRequestProperty("Accept-Language",Locale.getDefault().toLanguageTag());
            String data="source=NOTIFICATION&package_name="+enc(pkg)+"&title="+enc(title)+"&body="+enc(body);
            try(OutputStream os=c.getOutputStream()){ os.write(data.getBytes(StandardCharsets.UTF_8)); }
            c.getResponseCode(); c.disconnect();
        }catch(Exception ignored){}
    }
    private String enc(String s){ try{return URLEncoder.encode(s,"UTF-8");}catch(Exception e){return "";} }
}
