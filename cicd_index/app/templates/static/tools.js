function clicked_menu(id) {
    if (!id) {
        return;
    }
    window[id]();
}

function reload_table(table) {
    table.clearAll()
    table.load(table.config.url);
}

function reload_table_item($table, id, data) {
    debugger;
    var item = $table.getItem(id);
    if (item) {
        return;
    }
    for (var key in data) {
        if (data.hasOwnProperty(key)) {
            item[key] = data[key];
        }
    }
    $table.updateItem(item.id, item);
}
