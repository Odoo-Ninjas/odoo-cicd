var menu = {
    view: "menu",
    batch: "admin",
    autowidth: true,
    width: 120,
    type: {
        subsign: true,
    },
    data: [
        {
            id: "settings_mainmenu",
            view: "menu",
            value: "Admin...",
            config: { on: { onItemClick: clicked_menu}},
            submenu: [
                { $template:"Separator" },
                { view:"menu", id: "build_submenu", value: "Lifecycle", autowidth: true, config: { on: { onItemClick: clicked_menu}}, data: [
                    { view:"button", id:"restart", value:"Restart"},
                    { view:"button", id:"delete_instance", value:"Destroy (unrecoverable)", click: delete_instance_ask },
                ]
                },
                { $template:"Separator" },
                { view:"button", id:"reload_restart", value:"Reload & Restart" },
                { view:"button", id:"build_again", value:"Update recently changed modules" },
                { view:"button", id:"build_again_all", value:"Update all modules" },
                { view:"button", id:"rebuild", value:"Rebuild from Dump (Data lost)" },
                { $template:"Separator" },
                { view:"button", id:"backup_db", value:"Make Database Dump", click: backup_db },
                { $template:"Separator" },
                { view:"button", id:"turn_into_dev", value: 'Apply Developer Settings (Password, Cronjobs)', click: turn_into_dev}
            ]
        },
    ],
}

webix.ajax().get('/cicd/start_info').then(function(startinfo) {
    startinfo = startinfo.json();

    webix.ui(
            {
                view: "sidemenu",
                id: "sidemenu1",
                css: "webix_dark",
                body:{
                    view:"list",
                    borderless:true,
                    scroll: false,
                    template: "<span style='margin-right: 10px;' class='webix_icon fas fa-#icon#'></span> #value#",
                    on:{
                        onItemClick: clicked_menu,
                    },
                    data:[
                        { view:"button", id:"restart_delegator", icon: 'recycle', value:"Restart Delegator", batch: 'admin'},
                        { view:"button", id:"start_all", icon: 'play', value:"Start All Docker Containers", batch: 'admin', click: clicked_menu,},
                        { view:"button", id:"delete_unused", icon: 'eraser', value:"Spring Clean", batch: 'admin'},
                        { view:"button", id:"make_new_instance", icon: 'file', value:"New Instance", batch: 'admin'},
                        { view:"button", id:"users_admin", value:"Users", icon: "users", batch: 'admin' },
                        { view:"button", id:"appsettings", value:"App Settings", icon: "cog", batch: 'admin' },
                        { view:"button", id:"logout", value:"Logout", icon: "sign-out-alt", batch: 'user'},
                    ]
                }
            },
    );

    webix.ui({
        type: 'wide',
        cols: [
            {
                rows: [
                    {view: "toolbar", id:"toolbar_header", elements:[
                        {

                            view: "icon", icon: "fas fa-bars",
                            click: function(){
                                if( $$("sidemenu1").config.hidden){
                                    $$("sidemenu1").show(false, false);
                                }
                                else
                                    $$("sidemenu1").hide();
                            }
                        },
                        {
                            view: "template",
                            type: "header",
                            css: "webix_dark",
                            template: "CICD Feature Branches"
                        },
                    ]},
                    {
                        id: "resources-view",
                        view: "template",
                        type: "header",
                        template: "<div id='resources'></div>",
                    },
                    {
                        cols: [
                            {
                                cols: [
                                    { view: "label", label: "Show Archived"}, 
                                    { view: "checkbox", id: 'show_archived', click: update_sites },
                                ],
                            },
                            {
                            view:"text", 
                            placeholder:"Filter grid",
                            on:{
                                onTimedKeyPress:function(){
                                var text = this.getValue().toLowerCase();
                                var table = $$("table-sites");
                                var columns = table.config.columns;
                                table.filter(function(obj){
                                    for (var i=0; i<columns.length; i++) {
                                        if (obj[columns[i].id]) {
                                            if (obj[columns[i].id].toString().toLowerCase().indexOf(text) !== -1) return true;
                                        }
                                    }
                                    return false;
                                })
                                }
                            }
                        }]
                    },
                    {
                        id: 'table-sites',
                        view: "datatable",
                        navigation: true,
                        headerRowHeight: 60,
                        rowHeight: 40,
                        select: 'row',
                        autoConfig: false,
                        url: '/cicd/data/sites',
                        editable: false,
                        data: [],
                        leftSplit: 0,
                        scrollX: false,
                        scheme:{
                            $change/*or $init / $update*/: function(obj){
                              if(obj.build_state === 'Building....'){
                                obj.$css = "state-building";
                              }else{
                                obj.$css = "";
                              }
                            }
                          },
                        on: {
                            onSelectChange:function(){
                                if (!this.getSelectedItem()) {
                                    return;
                                }
                                reload_details(this.getSelectedItem().name);
                            },
                            onItemClick: function(id, e, trg) {
                                var name = this.getSelectedItem().name;
                                if (id.column === 'copy_to_clipboard') {
                                    var link = window.location.protocol + "//" + window.location.hostname + "/cicd/start?name=" + name;
                                    copyTextToClipboard(link);
                                }
                                else if (id.column === 'live_log') {
                                    var name = this.getSelectedItem().name;
                                    window.open("/cicd/live_log?name=" + name);
                                }
                            },
                            onKeyPress: function(code, e) {
                                var name = this.getSelectedItem() && this.getSelectedItem().name;
                                if (code == 68) {  // d
                                    if (confirm("Delete " + name)) {
                                        delete_instance(name);
                                    }
                                }
                                if (code == 82) { // r
                                    reload_restart(name);
                                }
                            },
                            onBeforeEditStart:function(id){
                                var item = this.getItem(id.row);
                                return false;
                             }
                        },
                        columns:[
                            { id: 'copy_to_clipboard', header: '',  template: "html->clipboard-icon" }, 
                            //{ id: 'live_log', header: '',  template: "html->live_log-icon" }, 
                            { id: 'name', header: 'Name', minWidth: 150},
                            //{ id: 'title', header: 'Title', minWidth: 180},
                            { id: 'build_state', header: 'Build', disable: true, minWidth: 80, readonly: true},
                            // { id: 'docker_state', header: 'Docker', },
                            { id: 'db_size_humanize', header: "DB Size", },
                            { id: 'source_size_humanize', header: "Source Size", },
                            //{ id: 'updated', header: 'Updated', minWidth: 150,},
                            { id: 'duration', header: 'Duration [s]'},
                        ],
                    },
                ]
            },
            {
            rows: [
                {
                    view: 'toolbar',
                    css: "webix_dark",
                    id: 'site-toolbar',
                    hidden: true,
                    elements: [
                        menu,
                        { view:"button", id:"build_log", value:"Build Log", width:150, align:"left", click: build_log, batch: 'admin' },
                        { view:"button", id:"start", value:"Open UI", width:100, align:"right", click: start_instance, batch: 'user' },
                        { view:"button", id:"start_mails", value:"Mails", width:100, align:"right", click: show_mails, batch: 'user' },
                        { view:"button", id:"start_logging", value:"Log Output", width:100, align:"right", click: show_logs, batch: 'admin' },
                        { view:"button", id:"start_shell", value:"Shell", width:100, align:"right", click: shell, batch: 'admin' },
                        { view:"button", id:"start_debugging", value:"Debug", width:100, align:"right", click: debug, batch: 'admin' },
                    ],
                },
                {% include "static/index_ui_siteform.js" %}
            ]
            }
        ]
    });


    webix.ui.fullScreen();

    if (!startinfo.is_admin) {
        $$("site-toolbar").showBatch('admin', false);
        $$("site-toolbar-common").showBatch('admin', false);
        $$("sidemenu1").showBatch('admin', false);
    }

});