package com.azansip.tv;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/** Hands the downloaded update (Updater) to Android's installer: content://com.azansip.tv.apk/AzanTV.apk.
 *  Not exported; the installer reads it only with the permission the install request gives it. */
public class ApkProvider extends ContentProvider {
    static final String AUTHORITY = "com.azansip.tv.apk";
    static final String NAME = "AzanTV.apk";

    static Uri uri() {
        return Uri.parse("content://" + AUTHORITY + "/" + NAME);
    }

    private File file() {
        return Updater.file(getContext());
    }

    @Override public boolean onCreate() {
        return true;
    }

    @Override public String getType(Uri uri) {
        return Updater.TYPE;
    }

    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!NAME.equals(uri.getLastPathSegment()) || !"r".equals(mode)) throw new FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(file(), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        MatrixCursor c = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        File f = file();
        if (f.isFile()) c.addRow(new Object[]{NAME, f.length()});
        return c;
    }

    @Override public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
