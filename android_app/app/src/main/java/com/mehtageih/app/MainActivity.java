package com.mehtageih.app;

import android.app.*;
import android.os.*;
import android.content.*;
import android.graphics.Color;
import android.view.*;
import android.widget.*;
import android.webkit.*;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    WebView web;
    android.content.SharedPreferences prefs;

    @Override public void onCreate(Bundle b){
        super.onCreate(b);
        prefs=getSharedPreferences("app",MODE_PRIVATE);
        String backend=prefs.getString("backend_url","");
        if(backend.isEmpty()) { showSetup(); return; }
        showWeb(backend);
    }

    private void showSetup(){
        LinearLayout box=new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(48,80,48,48);
        box.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        TextView h=new TextView(this); h.setText("محتاج إيه؟"); h.setTextSize(30); h.setTextColor(Color.BLACK);
        TextView p=new TextView(this); p.setText("أدخل رابط السيرفر الآمن HTTPS مرة واحدة. بعد كده التطبيق هيفتح عليه تلقائيًا."); p.setTextSize(16); p.setPadding(0,20,0,20);
        EditText url=new EditText(this); url.setHint("https://example.com"); url.setInputType(android.text.InputType.TYPE_TEXT_VARIATION_URI);
        Button save=new Button(this); save.setText("تشغيل التطبيق");
        save.setOnClickListener(v->{
            String u=url.getText().toString().trim();
            if(!u.startsWith("https://")){ Toast.makeText(this,"الرابط لازم يبدأ بـ https://",Toast.LENGTH_LONG).show(); return; }
            while(u.endsWith("/")) u=u.substring(0,u.length()-1);
            prefs.edit().putString("backend_url",u).apply(); showWeb(u);
        });
        box.addView(h); box.addView(p); box.addView(url); box.addView(save);
        setContentView(box);
    }

    private void showWeb(String backend){
        web=new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.setWebViewClient(new WebViewClient());
        setContentView(web);
        Intent intent=getIntent();
        if(Intent.ACTION_SEND.equals(intent.getAction()) && intent.getType()!=null && intent.getType().startsWith("text/")){
            String shared=intent.getStringExtra(Intent.EXTRA_TEXT);
            if(shared!=null){
                String u=backend+"/share?text="+URLEncoder.encode(shared, StandardCharsets.UTF_8);
                web.loadUrl(u); return;
            }
        }
        web.loadUrl(backend+"/");
    }

    @Override public void onBackPressed(){
        if(web!=null && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
