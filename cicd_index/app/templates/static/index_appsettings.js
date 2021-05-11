function appsettings(){
    webix.ajax().get('/cicd/data/app_settings', {}).then(function(data) {
        data = data.json();
        var form = webix.ui({
            view: "window", 
            position: 'center',
            modal: true,
            head: "Settings",
            width: 550,
            body: {
                view: 'form',
                complexData: true,
                elements: [
                    { view: 'text', name: 'concurrent_builds', label: "Concurrent Builds" },
                    {
                        cols:[
                            { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                webix.ajax().post('/cicd/data/app_settings', values).then(function(data) {
                                    form.hide();
                                    values = data.json();
                                });
                                }
                            },
                            { view:"button", value:"Cancel", click: function() {
                                form.hide();
                            }}
                        ]
                    }
                ],
                on: {
                    'onSubmit': function() {
                    },
                }
            }
        });
        form.getChildViews()[1].setValues(data);
        form.show();
    });
    return false;
}